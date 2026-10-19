"""Strict parsers for numeric identifiers embedded in callback data."""


_MAX_DATABASE_INTEGER = 2_147_483_647


def _parse_positive_callback_id(data: str | None, prefix: str) -> int | None:
    if not data or not data.startswith(prefix):
        return None
    raw_id = data.removeprefix(prefix)
    if not raw_id.isascii() or not raw_id.isdecimal():
        return None
    mafile_id = int(raw_id)
    return mafile_id if 0 < mafile_id <= _MAX_DATABASE_INTEGER else None


def _parse_nonnegative_callback_id(data: str | None, prefix: str) -> int | None:
    """Parse one exact ASCII decimal callback field within database bounds."""
    if not data or not data.startswith(prefix):
        return None
    raw_id = data.removeprefix(prefix)
    if not raw_id or not raw_id.isascii() or not raw_id.isdecimal():
        return None
    value = int(raw_id)
    return value if value <= _MAX_DATABASE_INTEGER else None
