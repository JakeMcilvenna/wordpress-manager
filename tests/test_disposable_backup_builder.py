"""Disposable snapshot bundle builder regression checks."""
from hashlib import sha256
from io import BytesIO
import tarfile

import pytest

from wordpress_manager.backup_archive import inspect_backup_bundle
from wordpress_manager.disposable_backup_builder import build_disposable_backup_bundle

SITE = "wm-firstsite001"


def test_disposable_bundle_round_trip_and_determinism():
    files = {"wp-content/index.php": b"<?php", "wp-config.php": b"<?php // disposable"}
    data, manifest = build_disposable_backup_bundle(SITE, files, b"-- disposable sql")
    again, second = build_disposable_backup_bundle(SITE, dict(reversed(list(files.items()))), b"-- disposable sql")
    assert data == again and manifest == second
    assert manifest["archive_sha256"] == sha256(data).hexdigest()
    inspect_backup_bundle(SITE, manifest["archive_bytes"], manifest["archive_sha256"], BytesIO(data))
    with tarfile.open(fileobj=BytesIO(data), mode="r:") as archive:
        assert archive.extractfile(f"files/{SITE}/wp-config.php").read() == files["wp-config.php"]
        assert archive.extractfile(f"database/{SITE}.sql").read() == b"-- disposable sql"


@pytest.mark.parametrize("files", [
    {},
    {"index.php": b"x"},
    {"wp-config.php": b""},
    {"wp-config.php": b"x", "../other/config.php": b"bad"},
    {"wp-config.php": b"x", "/etc/passwd": b"bad"},
    {"wp-config.php": b"x", "dir//name": b"bad"},
    {"wp-config.php": b"x", "dir\\name": b"bad"},
    {"wp-config.php": b"x", "wp-content": b"x", "wp-content/index.php": b"x"},
    {"wp-config.php": b"x", "binary": "not bytes"},
])
def test_disposable_builder_rejects_unsafe_snapshots(files):
    with pytest.raises(ValueError):
        build_disposable_backup_bundle(SITE, files, b"-- sql")


def test_disposable_builder_rejects_cross_site_and_oversize():
    with pytest.raises(ValueError):
        build_disposable_backup_bundle("../other", {"wp-config.php": b"x"}, b"-- sql")
    with pytest.raises(ValueError):
        build_disposable_backup_bundle(SITE, {"wp-config.php": b"x"}, b"x" * (64 * 1024 * 1024 + 1))
