import io
import struct
import zipfile

import pytest

from guardbot.archive_security import ArchiveSecurityError, validate_zip_entry_count


def _build_zip(entry_count: int) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for index in range(entry_count):
            archive.writestr(f"ignored-{index}.txt", b"")
    return buffer.getvalue()


def _build_non_sentinel_zip64_bypass() -> bytes:
    payload = _build_zip(501)
    eocd_offset = payload.rfind(b"PK\x05\x06")
    central_size, central_offset = struct.unpack_from("<LL", payload, eocd_offset + 12)

    fake_entry = bytearray(payload[central_offset : central_offset + 46])
    zip64_metadata_size = 56 + 20
    struct.pack_into("<HHH", fake_entry, 28, 0, 0, zip64_metadata_size)

    zip64_offset = eocd_offset + len(fake_entry)
    authoritative_entries = 502
    authoritative_central_size = central_size + len(fake_entry)
    zip64_eocd = struct.pack(
        "<4sQ2H2L4Q",
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        authoritative_entries,
        authoritative_entries,
        authoritative_central_size,
        central_offset,
    )
    zip64_locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, zip64_offset, 1)

    legacy_eocd = bytearray(payload[eocd_offset:])
    struct.pack_into("<HH", legacy_eocd, 8, 1, 1)
    struct.pack_into(
        "<LL",
        legacy_eocd,
        12,
        len(fake_entry) + zip64_metadata_size,
        eocd_offset,
    )
    return (
        payload[:eocd_offset]
        + fake_entry
        + zip64_eocd
        + zip64_locator
        + legacy_eocd
    )


def test_archive_rejects_excessive_total_entry_count_before_opening() -> None:
    payload = _build_zip(501)

    with pytest.raises(ArchiveSecurityError):
        validate_zip_entry_count(payload, max_entries=500)


def test_archive_accepts_bounded_entry_count() -> None:
    assert validate_zip_entry_count(_build_zip(2), max_entries=500) == 2


def test_archive_rejects_forged_eocd_entry_count() -> None:
    payload = bytearray(_build_zip(501))
    eocd_offset = payload.rfind(b"PK\x05\x06")
    assert eocd_offset >= 0
    struct.pack_into("<H", payload, eocd_offset + 8, 1)
    struct.pack_into("<H", payload, eocd_offset + 10, 1)

    with pytest.raises(ArchiveSecurityError):
        validate_zip_entry_count(bytes(payload), max_entries=500)


def test_archive_rejects_non_sentinel_zip64_metadata() -> None:
    payload = _build_non_sentinel_zip64_bypass()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert len(archive.infolist()) == 502

    with pytest.raises(ArchiveSecurityError):
        validate_zip_entry_count(payload, max_entries=500)
