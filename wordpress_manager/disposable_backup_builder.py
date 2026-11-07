"""Disposable-only USTAR backup bundle assembly from trusted in-memory snapshots.

This is a pure packaging primitive, not a filesystem backup or privileged
operation. Callers must independently acquire a consistent WordPress/database
snapshot under an ownership lock. Never accept browser-supplied file paths.
"""
from hashlib import sha256
from io import BytesIO
import re
import tarfile

from .backup_archive import inspect_backup_bundle

_MAX_TOTAL = 64 * 1024 * 1024


def build_disposable_backup_bundle(site_id, wordpress_files, sql_dump):
    """Return (archive_bytes, manifest) for a small disposable test snapshot.

    wordpress_files maps site-relative POSIX paths to immutable bytes. This
    deliberately rejects symlinks, filesystem handles and arbitrary tar headers.
    Large production sites require a separately reviewed streaming design.
    """
    if not isinstance(site_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{2,63}", site_id):
        raise ValueError("Invalid site ID")
    if not isinstance(wordpress_files, dict) or not wordpress_files:
        raise ValueError("WordPress snapshot files required")
    if type(sql_dump) is not bytes or not sql_dump:
        raise ValueError("Nonempty SQL snapshot bytes required")
    if "wp-config.php" not in wordpress_files:
        raise ValueError("WordPress configuration snapshot required")
    total = len(sql_dump)
    normalized = {}
    for path, content in wordpress_files.items():
        if (not isinstance(path, str) or not path or path.startswith("/")
                or "\\" in path or any(p in ("", ".", "..") for p in path.split("/"))
                or any(ord(c) < 32 or ord(c) == 127 for c in path)
                or type(content) is not bytes):
            raise ValueError("Unsafe snapshot path or content")
        if len(path.encode("utf-8")) > 100:
            raise ValueError("Snapshot path exceeds portable USTAR limit")
        if path in normalized:
            raise ValueError("Duplicate snapshot path")
        normalized[path] = content
        total += len(content)
        if total > _MAX_TOTAL:
            raise ValueError("Disposable snapshot exceeds memory budget")
    if not normalized["wp-config.php"]:
        raise ValueError("Empty WordPress configuration snapshot")
    for path in normalized:
        parent = path.rpartition("/")[0]
        while parent:
            if parent in normalized:
                raise ValueError("File/directory collision")
            parent = parent.rpartition("/")[0]
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for path in sorted(normalized):
            payload = normalized[path]
            member = tarfile.TarInfo("files/" + site_id + "/" + path)
            member.size = len(payload)
            member.mode = 0o600 if path == "wp-config.php" else 0o644
            member.mtime = 0
            archive.addfile(member, BytesIO(payload))
        sql_member = tarfile.TarInfo("database/" + site_id + ".sql")
        sql_member.size = len(sql_dump)
        sql_member.mode = 0o600
        sql_member.mtime = 0
        archive.addfile(sql_member, BytesIO(sql_dump))
    data = stream.getvalue()
    manifest = {"site_id": site_id, "archive_bytes": len(data),
                "archive_sha256": sha256(data).hexdigest()}
    inspect_backup_bundle(site_id, len(data), manifest["archive_sha256"], BytesIO(data))
    return data, manifest
