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
import tarfile
import urllib.parse
import urllib.request

MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024


def unpack(data, expected, destination):
    if not re.fullmatch(r"[a-f0-9]{64}", expected) or hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Release SHA256 mismatch")
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Extraction destination must not exist")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        names = set()
        total = 0
        for member in members:
            path = PurePosixPath(member.name)
            if (not member.isfile() or path.is_absolute() or ".." in path.parts
                    or "\\" in member.name or member.name in names or member.size < 0):
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
    actual = {p.relative_to(source).as_posix() for p in source.rglob("*") if p.is_file()}
    if actual != set(files) | {"release-manifest.json"}:
        raise ValueError("Release file inventory mismatch")
    for name, expected in files.items():
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise ValueError("Unsafe manifest path")
        item = source / name
        if item.is_symlink() or hashlib.sha256(item.read_bytes()).hexdigest() != expected:
            raise ValueError("Release file checksum mismatch: " + name)
    return manifest


def build(source, output):
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
    fetch = sub.add_parser("fetch")
    fetch.add_argument("url")
    fetch.add_argument("sha256")
    fetch.add_argument("destination")
    args = parser.parse_args()
    if args.command == "build":
        print(build(Path(__file__).resolve().parents[1], args.output))
    else:
        unpack(download(args.url), args.sha256, args.destination)
