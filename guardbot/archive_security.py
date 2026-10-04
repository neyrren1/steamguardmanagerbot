"""Bounded ZIP metadata checks that run before ``zipfile`` allocates entries."""

import struct


class ArchiveSecurityError(ValueError):
    """Raised when archive metadata exceeds safe processing limits."""


_EOCD_SIGNATURE = b"PK\x05\x06"
_CENTRAL_DIRECTORY_SIGNATURE = b"PK\x01\x02"
_ZIP64_LOCATOR_SIGNATURE = b"PK\x06\x07"
_EOCD_MIN_SIZE = 22
_CENTRAL_DIRECTORY_HEADER_SIZE = 46
_MAX_COMMENT_SIZE = 65_535


def _find_eocd(payload: bytes) -> tuple[int, tuple[int, ...]]:
    """Return the structurally valid EOCD, ignoring signatures inside comments."""
    search_start = max(0, len(payload) - _EOCD_MIN_SIZE - _MAX_COMMENT_SIZE)
    offset = len(payload)
    while True:
        offset = payload.rfind(_EOCD_SIGNATURE, search_start, offset)
        if offset < 0:
            raise ArchiveSecurityError("ZIP end-of-central-directory record not found")
        if offset + _EOCD_MIN_SIZE <= len(payload):
            fields = struct.unpack_from("<4H2LH", payload, offset + 4)
            comment_length = fields[-1]
            if offset + _EOCD_MIN_SIZE + comment_length == len(payload):
                return offset, fields


def validate_zip_entry_count(
    payload: bytes | bytearray | memoryview, *, max_entries: int
) -> int:
    """Bound and verify central-directory records before ``zipfile`` opens them."""
    raw_payload = bytes(payload)
    eocd_offset, fields = _find_eocd(raw_payload)
    if (
        eocd_offset >= 20
        and raw_payload[eocd_offset - 20 : eocd_offset - 16]
        == _ZIP64_LOCATOR_SIGNATURE
    ):
        raise ArchiveSecurityError("ZIP64 archives are not supported")
    (
        disk_number,
        central_disk,
        entries_on_disk,
        total_entries,
        central_size,
        central_offset,
        _comment_length,
    ) = fields
    if disk_number != 0 or central_disk != 0 or entries_on_disk != total_entries:
        raise ArchiveSecurityError("Multi-disk ZIP archives are not supported")
    if (
        total_entries == 0xFFFF
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
    ):
        raise ArchiveSecurityError("ZIP64 archives are not supported")

    # ``zipfile`` supports prepended data. Account for it while requiring the
    # central directory to end exactly where the EOCD begins.
    prefix_size = eocd_offset - central_size - central_offset
    if prefix_size < 0:
        raise ArchiveSecurityError("Invalid ZIP central directory")
    position = central_offset + prefix_size
    central_end = position + central_size
    if central_end != eocd_offset:
        raise ArchiveSecurityError("Invalid ZIP central directory")

    actual_entries = 0
    while position < central_end:
        if (
            position + _CENTRAL_DIRECTORY_HEADER_SIZE > central_end
            or raw_payload[position : position + 4]
            != _CENTRAL_DIRECTORY_SIGNATURE
        ):
            raise ArchiveSecurityError("Invalid ZIP central-directory entry")
        name_length, extra_length, comment_length = struct.unpack_from(
            "<HHH", raw_payload, position + 28
        )
        record_size = (
            _CENTRAL_DIRECTORY_HEADER_SIZE
            + name_length
            + extra_length
            + comment_length
        )
        if position + record_size > central_end:
            raise ArchiveSecurityError("Truncated ZIP central-directory entry")
        actual_entries += 1
        if actual_entries > max_entries:
            raise ArchiveSecurityError(
                f"Archive contains more than {max_entries} entries"
            )
        position += record_size

    if position != central_end or actual_entries != total_entries:
        raise ArchiveSecurityError("ZIP entry count does not match central directory")
    return actual_entries
