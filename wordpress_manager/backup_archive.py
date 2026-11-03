"""Read-only, streaming inspection of a proposed version-one Ubuntu backup bundle.

This deliberately does not extract files, restore databases, choose a backup
path, or authorize an operation. The caller must open an immutable archive from
its trusted backup ledger. A future privileged executor must revalidate under
an ownership lock, and restore transaction/rollback remain unimplemented.

The proposed format is an uncompressed USTAR archive containing regular files
under files/<site-id>/ and exactly one database/<site-id>.sql dump.
The WordPress wp-config.php must be present. No links, special files,
PAX/GNU extensions, duplicate names or unexpected archive members are allowed.
"""
from hashlib import sha256
from hmac import compare_digest
import re
import tarfile

_BLOCK = 512
_CHUNK = 1024 * 1024
_MAX_MEMBERS = 10000


def inspect_backup_bundle(site_id, archive_bytes, archive_sha256, source):
    """Validate checksum, byte length and safe USTAR layout without extraction.

    Values must come from a validated, trusted server-side manifest, never
    from browser-provided paths. Reads each byte once, in bounded chunks,
    without closing the caller's stream. This is *not* proof that SQL is valid,
    WordPress files are trustworthy, or the archive can safely be restored.
    """
    # These values must come from a trusted, validated backup manifest. Repeat
    # the narrow envelope checks here so direct calls also fail closed.
    if not isinstance(site_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{2,63}", site_id):
        raise ValueError("Invalid owned backup site ID")
    if type(archive_bytes) is not int or not 2 * _BLOCK <= archive_bytes <= 1024 ** 4 or archive_bytes % _BLOCK:
        raise ValueError("Invalid USTAR archive length")
    if not isinstance(archive_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", archive_sha256):
        raise ValueError("Invalid backup archive checksum")
    reader = getattr(source, "read", None)
    if not callable(reader):
        raise ValueError("Trusted binary backup stream required")

    digest = sha256()
    remaining = archive_bytes

    def read_exact(amount):
        nonlocal remaining
        if amount < 0 or amount > remaining:
            raise ValueError("Backup archive truncated or malformed")
        chunks = []
        while amount:
            requested = min(amount, _CHUNK)
            chunk = reader(requested)
            if type(chunk) is not bytes or not 1 <= len(chunk) <= requested:
                raise ValueError("Backup archive size or binary stream mismatch")
            digest.update(chunk)
            remaining -= len(chunk)
            amount -= len(chunk)
            chunks.append(chunk)
        return b"".join(chunks)

    def skip_exact(amount):
        while amount:
            chunk = min(amount, _CHUNK)
            read_exact(chunk)
            amount -= chunk

    prefix = "files/" + site_id + "/"
    sql = "database/" + site_id + ".sql"
    seen = set()
    regular_files = set()
    member_count = 0
    found_end = False

    while remaining:
        block = read_exact(_BLOCK)
        if block == b"\0" * _BLOCK:
            if remaining < _BLOCK or read_exact(_BLOCK) != b"\0" * _BLOCK:
                raise ValueError("Invalid USTAR end marker")
            # A second tar archive or any hidden nonzero trailer is forbidden.
            while remaining:
                if any(read_exact(min(remaining, _CHUNK))):
                    raise ValueError("Unexpected data after USTAR end marker")
            found_end = True
            break
        if block[257:263] != b"ustar\0":
            raise ValueError("Only uncompressed USTAR archives are supported")
        try:
            member = tarfile.TarInfo.frombuf(block, encoding="utf-8", errors="strict")
        except (tarfile.TarError, ValueError, UnicodeError, OverflowError):
            raise ValueError("Invalid USTAR member header") from None
        member_count += 1
        if member_count > _MAX_MEMBERS:
            raise ValueError("Too many backup archive members")
        if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE):
            raise ValueError("Backup archive links and special files are forbidden")
        # Reject permissions that could let another site account modify a
        # restored tree, plus sticky/setuid/setgid bits. This is only archive
        # validation; a future restore must independently set safe ownership.
        if member.mode & 0o7002:
            raise ValueError("Unsafe backup archive file permissions")
        is_dir = member.isdir()
        name = member.name
        if is_dir and name.endswith("/"):
            name = name[:-1]
        parts = name.split("/")
        if (
            not name or name.startswith("/") or "\\" in name
            or any(not part or part in (".", "..") for part in parts)
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or name in seen
        ):
            raise ValueError("Unsafe or duplicate backup archive path")
        seen.add(name)
        if is_dir:
            if member.size != 0 or not (
                name in ("files", "files/" + site_id, "database")
                or name.startswith(prefix)
            ):
                raise ValueError("Unexpected backup archive directory")
        elif not (name.startswith(prefix) and len(name) > len(prefix) or name == sql):
            raise ValueError("Archive member does not belong to the selected site")
        else:
            if name in (prefix + "wp-config.php", sql) and member.size == 0:
                raise ValueError("Empty required WordPress backup member")
            regular_files.add(name)
        padded = (member.size + _BLOCK - 1) // _BLOCK * _BLOCK
        if member.size < 0 or padded > remaining:
            raise ValueError("Backup archive member exceeds declared size")
        skip_exact(padded)

    if not found_end:
        raise ValueError("Missing USTAR end marker")
    if prefix + "wp-config.php" not in regular_files or sql not in regular_files:
        raise ValueError("Incomplete WordPress backup bundle")
    # A regular file may not also be the parent of another member.
    for path in seen:
        parent = path.rpartition("/")[0]
        while parent:
            if parent in regular_files:
                raise ValueError("Backup archive file/directory collision")
            parent = parent.rpartition("/")[0]
    extra = reader(1)
    if type(extra) is not bytes or extra:
        raise ValueError("Backup archive size or binary stream mismatch")
    if not compare_digest(digest.hexdigest(), archive_sha256):
        raise ValueError("Backup archive SHA-256 mismatch")
