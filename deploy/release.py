#!/usr/bin/python3
"""Build deterministic source archives; verify and unpack without executing them.

The SHA256 must arrive through a trusted channel, independently of the download.
No repository credentials are required by a published HTTPS artifact.
"""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile
import urllib.parse
import urllib.request

MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024


def unpack(data, expected, destination):
    if len(data) > MAX_ARCHIVE:
        raise ValueError("Release archive too large")
    if not re.fullmatch(r"[a-f0-9]{64}", expected) or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Release SHA256 mismatch")
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Extraction destination must not exist")
    # Bound the gzip stream itself, including tar/PAX metadata and padding, before
    # tarfile parses headers. Member-size checks alone do not bound PAX bombs.
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as zipped:
        expanded = zipped.read(MAX_EXPANDED + 1)
    if len(expanded) > MAX_EXPANDED:
        raise ValueError("Release archive too large")
    with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
        names = set()
        total = 0
        members = []
        for member in archive:
            members.append(member)
            path = PurePosixPath(member.name)
            if (not member.isfile() or path.is_absolute() or ".." in path.parts
                    or "\\" in member.name or member.name in names or member.size < 0
                    or str(path) != member.name or not path.parts):
                raise ValueError("Unsafe release archive member")
            names.add(member.name)
            total += member.size
            if len(members) > 10000 or total > MAX_EXPANDED:
                raise ValueError("Release archive too large")
        if not {"deploy/install.sh", "deploy/manage.py", "release-manifest.json"} <= names:
            raise ValueError("Incomplete release archive")
        destination.mkdir(mode=0o700)
        for member in members:
            path = destination / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.extractfile(member).read())
            path.chmod(0o755 if path.suffix == '.sh' else 0o644)
    verify_tree(destination)


def verify_tree(source):
    source = Path(source)
    manifest = json.loads((source / "release-manifest.json").read_text())
    files = manifest["files"]
    if not isinstance(files, dict) or not files:
        raise ValueError("Invalid file manifest")
    if 'git_commit' in manifest and not re.fullmatch(r'[a-f0-9]{40}', manifest['git_commit']):
        raise ValueError("Invalid Git commit")
    if any(p.is_symlink() for p in source.rglob('*')):
        raise ValueError("Release symlinks are forbidden")
    actual = {p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file()}
    if actual != set(files) | {"release-manifest.json"}:
        raise ValueError("Release file inventory mismatch")
    for name, expected in files.items():
        path = PurePosixPath(name)
        if (path.is_absolute() or ".." in path.parts or "\\" in name or str(path) != name
                or not re.fullmatch(r'[a-f0-9]{64}', expected)):
            raise ValueError("Unsafe manifest path")
        item = source / name
        if item.is_symlink() or hashlib.sha256(item.read_bytes()).hexdigest() != expected:
            raise ValueError("Release file checksum mismatch: " + name)
    return manifest


def build(source, output, commit=None):
    source = Path(source)
    files = {}
    paths = [source / "pyproject.toml", source / "README.md", source / 'SECURITY.md']
    for directory in ("wordpress_manager", "deploy", 'docs'):
        paths.extend((source / directory).rglob("*"))
    for path in sorted(paths):
        if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo") or path.is_dir():
            continue
        if path.is_symlink():
            raise ValueError("Release symlinks are forbidden")
        files[path.relative_to(source).as_posix()] = path.read_bytes().replace(b"\r\n", b"\n")
    import tomllib
    version = tomllib.loads(files["pyproject.toml"].decode())["project"]["version"]
    manifest = {"version": version, "files": {n: hashlib.sha256(b).hexdigest() for n, b in files.items()}}
    if commit is not None:
        if not re.fullmatch(r'[a-f0-9]{40}', commit):
            raise ValueError('A full Git commit SHA is required')
        manifest['git_commit'] = commit
    files["release-manifest.json"] = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    with Path(output).open("wb") as handle, gzip.GzipFile(fileobj=handle, mode="wb", filename="", mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w") as archive:
            for name, data in sorted(files.items()):
                info = tarfile.TarInfo(name)
                info.size, info.mode, info.mtime = len(data), (0o755 if name.endswith('.sh') else 0o644), 0
                archive.addfile(info, io.BytesIO(data))
    sha = hashlib.sha256(Path(output).read_bytes()).hexdigest()
    Path(str(output) + ".sha256").write_text(sha + "  " + Path(output).name + "\n")
    return sha


def build_commit(source, output, commit):
    """Export Git objects, never a mutable checkout (including dirty/untracked files)."""
    if not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ValueError('A full Git commit SHA is required')
    data = subprocess.run(['git', 'archive', '--format=tar', commit, '--',
                           'pyproject.toml', 'README.md', 'SECURITY.md',
                           'wordpress_manager', 'deploy', 'docs'], cwd=source,
                          check=True, capture_output=True).stdout
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            for member in archive:
                path = PurePosixPath(member.name)
                if (path.is_absolute() or '..' in path.parts or '\\' in member.name
                        or not (member.isdir() or member.isfile())):
                    raise ValueError('Unsafe Git source member')
                if member.isfile():
                    target = root / member.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.extractfile(member).read())
        return build(root, output, commit)


def download(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Use a public, version-pinned HTTPS URL without credentials or query parameters")
    with urllib.request.urlopen(url, timeout=60) as response:
        if urllib.parse.urlsplit(response.url).scheme != "https":
            raise ValueError("Insecure redirect")
        data = response.read(MAX_ARCHIVE + 1)
    if len(data) > MAX_ARCHIVE:
        raise ValueError("Release download too large")
    return data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    pack = sub.add_parser("build")
    pack.add_argument("output")
    pack.add_argument('--commit', help='Build only objects from this exact full Git commit')
    check = sub.add_parser('verify')
    check.add_argument('archive')
    check.add_argument('sha256')
    check.add_argument('--commit', required=True)
    fetch = sub.add_parser("fetch")
    fetch.add_argument("url")
    fetch.add_argument("sha256")
    fetch.add_argument("destination")
    args = parser.parse_args()
    if args.command == "build":
        source = Path(__file__).resolve().parents[1]
        print(build_commit(source, args.output, args.commit) if args.commit else build(source, args.output))
    elif args.command == 'verify':
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'release'
            unpack(Path(args.archive).read_bytes(), args.sha256, target)
            if verify_tree(target).get('git_commit') != args.commit:
                raise ValueError('Release commit mismatch')
    else:
        unpack(download(args.url), args.sha256, args.destination)
