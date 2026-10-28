import hashlib
import importlib.util
import io
import gzip
import os
from pathlib import Path
import tarfile

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_test", REPO / "deploy/release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_release_is_reproducible_and_checksum_verified(tmp_path):
    a, b = tmp_path / "a.tgz", tmp_path / "b.tgz"
    assert release.build(REPO, a) == release.build(REPO, b)
    output = tmp_path / "verified"
    release.unpack(a.read_bytes(), hashlib.sha256(a.read_bytes()).hexdigest(), output)
    release.verify_tree(output)
    if os.name == 'posix':
        assert (output / 'deploy/uninstall.sh').stat().st_mode & 0o111 == 0o111
    (output / "deploy/install.sh").write_text("tampered")
    with pytest.raises(ValueError, match="checksum mismatch"):
        release.verify_tree(output)
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        release.unpack(b.read_bytes(), "0" * 64, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()


@pytest.mark.parametrize("name,kind", [("../escape", tarfile.REGTYPE), ("/etc/escape", tarfile.REGTYPE),
                                       ("safe", tarfile.SYMTYPE), ("safe", tarfile.LNKTYPE)])
def test_unsafe_archives_are_rejected_before_extraction(tmp_path, name, kind):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        entry = tarfile.TarInfo(name)
        entry.type, entry.linkname = kind, "/etc/passwd"
        archive.addfile(entry)
    value = data.getvalue()
    with pytest.raises(ValueError, match="Unsafe"):
        release.unpack(value, hashlib.sha256(value).hexdigest(), tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("url", ["http://example.com/v1.tgz", "https://user:secret@example.com/v1.tgz", "https://example.com/v1.tgz?token=secret"])
def test_credentials_and_insecure_release_urls_rejected(url):
    with pytest.raises(ValueError, match="HTTPS URL"):
        release.download(url)


def test_cloud_init_embedded_python_and_safe_defaults():
    import ast
    import textwrap
    template = (REPO / 'deploy/cloud-init.yaml').read_text()
    body = template.split('    content: |\n      import hashlib', 1)[1].split('\nruncmd:', 1)[0]
    ast.parse(textwrap.dedent('      import hashlib' + body))
    assert '"provision_stack": false' in template
    assert 'PRIVATE KEY' not in template and 'github_pat_' not in template
    assert 'hashlib.sha256(data).hexdigest() != sha' in template
    assert template.index("run('/usr/bin/ssh-keygen'") < template.index("dropin.write_text(candidate)")


def test_compressed_metadata_bomb_is_bounded_before_tar_parsing(tmp_path, monkeypatch):
    monkeypatch.setattr(release, 'MAX_EXPANDED', 1024)
    data = gzip.compress(b'\0' * 2048)
    with pytest.raises(ValueError, match='too large'):
        release.unpack(data, hashlib.sha256(data).hexdigest(), tmp_path / 'bomb')
    assert not (tmp_path / 'bomb').exists()


@pytest.mark.parametrize('name', ['./deploy/install.sh', 'deploy//install.sh', 'deploy/../escape'])
def test_noncanonical_archive_paths_are_rejected(tmp_path, name):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w:gz') as archive:
        archive.addfile(tarfile.TarInfo(name))
    value = data.getvalue()
    with pytest.raises(ValueError, match='Unsafe'):
        release.unpack(value, hashlib.sha256(value).hexdigest(), tmp_path / 'out')
    assert not (tmp_path / 'out').exists()
