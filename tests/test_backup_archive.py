"""Hostile USTAR fixtures for read-only backup bundle inspection."""
from hashlib import sha256
from io import BytesIO
import tarfile

import pytest

from wordpress_manager.backup_archive import inspect_backup_bundle

SITE_ID = "wm-firstsite001"


def _archive(entries, *, format=tarfile.USTAR_FORMAT):
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=format) as tar:
        for name, content, kind, mode in entries:
            item = tarfile.TarInfo(name)
            item.mode = mode
            item.type = kind
            if kind == tarfile.REGTYPE:
                item.size = len(content)
                tar.addfile(item, BytesIO(content))
            else:
                tar.addfile(item)
    return stream.getvalue()


def _entries():
    return [
        (f"files/{SITE_ID}/wp-config.php", b"<?php // test only", tarfile.REGTYPE, 0o600),
        (f"files/{SITE_ID}/wp-content/index.php", b"<?php", tarfile.REGTYPE, 0o644),
        (f"database/{SITE_ID}.sql", b"-- disposable test dump", tarfile.REGTYPE, 0o600),
    ]


def test_inspection_accepts_valid_ustar_without_extracting_or_closing_stream():
    data = _archive(_entries())
    stream = BytesIO(data)
    assert inspect_backup_bundle(SITE_ID, len(data), sha256(data).hexdigest(), stream) is None
    assert not stream.closed
    assert stream.tell() == len(data)


@pytest.mark.parametrize("entries", [
    lambda e: e + [(f"files/{SITE_ID}/../other/wp-config.php", b"x", tarfile.REGTYPE, 0o600)],
    lambda e: e + [("files/wm-foreignsite001/wp-config.php", b"x", tarfile.REGTYPE, 0o600)],
    lambda e: e + [(f"files/{SITE_ID}/link", b"", tarfile.SYMTYPE, 0o777)],
    lambda e: e + [(f"files/{SITE_ID}/hardlink", b"", tarfile.LNKTYPE, 0o644)],
    lambda e: e + [(f"files/{SITE_ID}/device", b"", tarfile.CHRTYPE, 0o644)],
    lambda e: e + [(f"files/{SITE_ID}/wp-config.php", b"again", tarfile.REGTYPE, 0o600)],
    lambda e: e + [(f"files/{SITE_ID}/suid", b"x", tarfile.REGTYPE, 0o4755)],
    lambda e: [x for x in e if not x[0].endswith(".sql")],
    lambda e: [x for x in e if not x[0].endswith("wp-config.php")],
    lambda e: e + [("/etc/passwd", b"x", tarfile.REGTYPE, 0o600)],
    lambda e: e + [(f"files/{SITE_ID}/wp-config.php/child", b"x", tarfile.REGTYPE, 0o600)],
    lambda e: [(name, b"" if name.endswith(".sql") else content, kind, mode)
               for name, content, kind, mode in e],
    lambda e: [(name, b"" if name.endswith("wp-config.php") else content, kind, mode)
               for name, content, kind, mode in e],
])
def test_inspection_rejects_unsafe_or_incomplete_members(entries):
    data = _archive(entries(_entries()))
    with pytest.raises(ValueError):
        inspect_backup_bundle(SITE_ID, len(data), sha256(data).hexdigest(), BytesIO(data))


@pytest.mark.parametrize("alter", [
    lambda d: d[:-512],
    lambda d: d + b"X" * 512,
    lambda d: d[:1024] + b"X" + d[1025:],
    lambda d: d[:-512] + b"X" * 512,
])
def test_inspection_rejects_truncated_appended_corrupt_or_nonzero_trailer(alter):
    data = _archive(_entries())
    with pytest.raises(ValueError):
        inspect_backup_bundle(SITE_ID, len(data), sha256(data).hexdigest(), BytesIO(alter(data)))


def test_inspection_rejects_manifest_checksum_mismatch_and_nonbinary_stream():
    data = _archive(_entries())
    with pytest.raises(ValueError, match="SHA-256"):
        inspect_backup_bundle(SITE_ID, len(data), "b" * 64, BytesIO(data))

    class TextStream:
        def read(self, amount):
            return "text"

    with pytest.raises(ValueError, match="binary"):
        inspect_backup_bundle(SITE_ID, len(data), sha256(data).hexdigest(), TextStream())


def test_inspection_limits_read_requests_to_one_megabyte():
    data = _archive(_entries())

    class BoundedStream:
        def __init__(self):
            self.stream = BytesIO(data)
            self.max_request = 0

        def read(self, amount):
            self.max_request = max(self.max_request, amount)
            return self.stream.read(amount)

    stream = BoundedStream()
    inspect_backup_bundle(SITE_ID, len(data), sha256(data).hexdigest(), stream)
    assert stream.max_request <= 1024 * 1024


def test_inspection_rejects_gnu_and_pax_extended_headers():
    gnu = _archive(_entries(), format=tarfile.GNU_FORMAT)
    with pytest.raises(ValueError, match="USTAR"):
        inspect_backup_bundle(SITE_ID, len(gnu), sha256(gnu).hexdigest(), BytesIO(gnu))

    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, content, kind, mode in _entries():
            item = tarfile.TarInfo(name)
            item.type = kind
            item.mode = mode
            item.size = len(content)
            item.pax_headers = {"comment": "untrusted extended metadata"}
            tar.addfile(item, BytesIO(content))
    pax = stream.getvalue()
    with pytest.raises(ValueError):
        inspect_backup_bundle(SITE_ID, len(pax), sha256(pax).hexdigest(), BytesIO(pax))


@pytest.mark.parametrize("site_id,size,digest", [
    ("../other", 10240, "a" * 64),
    (SITE_ID, True, "a" * 64),
    (SITE_ID, 0, "a" * 64),
    (SITE_ID, 10241, "a" * 64),
    (SITE_ID, 10240, "A" * 64),
])
def test_invalid_envelope_is_rejected_before_stream_reads(site_id, size, digest):
    class NeverRead:
        def read(self, amount):
            raise AssertionError("Invalid metadata must fail before reading archive bytes")

    with pytest.raises(ValueError):
        inspect_backup_bundle(site_id, size, digest, NeverRead())


@pytest.mark.parametrize("unsafe_mode", [0o666, 0o664, 0o1777, 0o4755, 0o2755])
def test_inspection_rejects_unsafe_permissions_on_required_files(unsafe_mode):
    entries = _entries()
    name, content, kind, _ = entries[0]
    entries[0] = (name, content, kind, unsafe_mode)
    data = _archive(entries)
    with pytest.raises(ValueError, match="permissions"):
        inspect_backup_bundle(SITE_ID, len(data), sha256(data).hexdigest(), BytesIO(data))


@pytest.mark.parametrize("unsafe_mode", [0o777, 0o775, 0o1777])
def test_inspection_rejects_unsafe_permissions_on_directories(unsafe_mode):
    entries = _entries() + [(f"files/{SITE_ID}/uploads/", b"", tarfile.DIRTYPE, unsafe_mode)]
    data = _archive(entries)
    with pytest.raises(ValueError, match="permissions"):
        inspect_backup_bundle(SITE_ID, len(data), sha256(data).hexdigest(), BytesIO(data))


@pytest.mark.parametrize("member_name", [
    f"files/{SITE_ID}/wp-config.php",
    f"database/{SITE_ID}.sql",
])
def test_inspection_rejects_nonzero_ustar_member_padding(member_name):
    data = bytearray(_archive(_entries()))
    with tarfile.open(fileobj=BytesIO(data), mode="r:", format=tarfile.USTAR_FORMAT) as tar:
        member = tar.getmember(member_name)
        padding_offset = member.offset_data + member.size
        assert member.size % 512
        assert data[padding_offset] == 0
    data[padding_offset] = 0x58
    changed = bytes(data)
    with pytest.raises(ValueError, match="padding"):
        inspect_backup_bundle(
            SITE_ID, len(changed), sha256(changed).hexdigest(), BytesIO(changed)
        )


def test_inspection_accepts_full_block_member_without_padding():
    entries = _entries()
    name, _, kind, mode = entries[1]
    entries[1] = (name, b"x" * 512, kind, mode)
    data = _archive(entries)
    assert inspect_backup_bundle(
        SITE_ID, len(data), sha256(data).hexdigest(), BytesIO(data)
    ) is None


@pytest.mark.parametrize("field,value", [
    ("uid", 1001),
    ("gid", 1002),
    ("uname", "foreign-account"),
    ("gname", "foreign-group"),
    ("linkname", "foreign-target"),
])
def test_inspection_rejects_untrusted_ustar_ownership_and_device_metadata(field, value):
    stream = BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, content, kind, mode in _entries():
            member = tarfile.TarInfo(name)
            member.mode = mode
            member.type = kind
            member.size = len(content)
            if name.endswith("wp-config.php"):
                setattr(member, field, value)
            tar.addfile(member, BytesIO(content))
    data = stream.getvalue()
    with pytest.raises(ValueError, match="ownership or device metadata"):
        inspect_backup_bundle(SITE_ID, len(data), sha256(data).hexdigest(), BytesIO(data))


@pytest.mark.parametrize("offset", [329, 337])
def test_inspection_rejects_forged_ustar_device_numbers_on_regular_files(offset):
    # Python's tarfile writer normalizes device numbers on regular files.
    # Forge a syntactically valid raw USTAR header instead.
    data = bytearray(_archive(_entries()))
    data[offset:offset + 8] = b"0000001\0"
    data[148:156] = b" " * 8
    checksum = sum(data[:512])
    data[148:156] = f"{checksum:06o}\0 ".encode("ascii")
    forged = bytes(data)
    with pytest.raises(ValueError, match="ownership or device metadata"):
        inspect_backup_bundle(SITE_ID, len(forged), sha256(forged).hexdigest(), BytesIO(forged))


@pytest.mark.parametrize("offset,length", [
    (0, 100),     # filename
    (157, 100),   # link target (even for a regular file)
    (265, 32),    # user name
    (297, 32),    # group name
    (345, 155),   # path prefix
])
def test_inspection_rejects_hidden_text_after_ustar_nul(offset, length):
    # Forge a valid-checksum header with bytes concealed after a NUL.
    # Python's tar parser may discard the suffix, but another reader may not.
    data = bytearray(_archive(_entries()))
    field = bytes(data[offset:offset + length])
    nul = field.find(b"\0")
    assert nul >= 0 and nul + 1 < length
    data[offset + nul + 1] = ord("X")
    data[148:156] = b" " * 8
    checksum = sum(data[:512])
    data[148:156] = f"{checksum:06o}\0 ".encode("ascii")
    forged = bytes(data)
    with pytest.raises(ValueError, match="Noncanonical USTAR text field"):
        inspect_backup_bundle(SITE_ID, len(forged), sha256(forged).hexdigest(), BytesIO(forged))


@pytest.mark.parametrize("offset,mutated,expected", [
    (263, ord("X"), "version"),
    (264, ord("1"), "version"),
    (500, ord("X"), "reserved"),
    (511, ord("X"), "reserved"),
])
def test_inspection_rejects_noncanonical_ustar_version_and_reserved_bytes(offset, mutated, expected):
    # Keep the header checksum valid so the parser must enforce the format.
    data = bytearray(_archive(_entries()))
    data[offset] = mutated
    data[148:156] = b" " * 8
    checksum = sum(data[:512])
    data[148:156] = f"{checksum:06o}\0 ".encode("ascii")
    forged = bytes(data)
    with pytest.raises(ValueError, match=expected):
        inspect_backup_bundle(SITE_ID, len(forged), sha256(forged).hexdigest(), BytesIO(forged))
