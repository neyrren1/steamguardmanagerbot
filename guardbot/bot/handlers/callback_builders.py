"""Bounded Telegram callback-data builders shared by handler routers."""

_MAX_DATABASE_INTEGER = 2_147_483_647
_MAX_UNSIGNED_BIGINT = 18_446_744_073_709_551_615


def _bounded_identifier(value, maximum: int) -> str | None:
    if type(value) is int:
        numeric = value
    elif isinstance(value, str) and value and value.isascii() and value.isdecimal():
        numeric = int(value)
    else:
        return None
    if not 0 < numeric <= maximum:
        return None
    return str(numeric)


def _item_detail_callback(mafile_id, app_id, assetid) -> str | None:
    db_id = _bounded_identifier(mafile_id, _MAX_DATABASE_INTEGER)
    app = _bounded_identifier(app_id, _MAX_DATABASE_INTEGER)
    asset = _bounded_identifier(assetid, _MAX_UNSIGNED_BIGINT)
    if None in {db_id, app, asset}:
        return None
    payload = f"item_detail_{db_id}_{app}_{asset}"
    return payload if len(payload.encode("utf-8")) <= 64 else None


def _confirmation_callback(action: str, mafile_id, confirmation_id) -> str | None:
    if action not in {"confirm", "cancel"}:
        return None
    db_id = _bounded_identifier(mafile_id, _MAX_DATABASE_INTEGER)
    conf_id = _bounded_identifier(confirmation_id, _MAX_UNSIGNED_BIGINT)
    if None in {db_id, conf_id}:
        return None
    payload = f"{action}_single_{db_id}_{conf_id}"
    return payload if len(payload.encode("utf-8")) <= 64 else None


def _trade_accept_callback(mafile_id, tradeofferid) -> str | None:
    db_id = _bounded_identifier(mafile_id, _MAX_DATABASE_INTEGER)
    trade_id = _bounded_identifier(tradeofferid, _MAX_UNSIGNED_BIGINT)
    if None in {db_id, trade_id}:
        return None
    payload = f"trade_accept_{db_id}_{trade_id}"
    return payload if len(payload.encode("utf-8")) <= 64 else None


def _trade_detail_callback(mafile_id, tradeofferid, url_type: str) -> str | None:
    if url_type not in {"incoming", "outgoing"}:
        return None
    db_id = _bounded_identifier(mafile_id, _MAX_DATABASE_INTEGER)
    trade_id = _bounded_identifier(tradeofferid, _MAX_UNSIGNED_BIGINT)
    if None in {db_id, trade_id}:
        return None
    payload = f"trade_detail_full_{db_id}_{trade_id}_{url_type}"
    return payload if len(payload.encode("utf-8")) <= 64 else None


def _trade_decline_callback(mafile_id, tradeofferid) -> str | None:
    db_id = _bounded_identifier(mafile_id, _MAX_DATABASE_INTEGER)
    trade_id = _bounded_identifier(tradeofferid, _MAX_UNSIGNED_BIGINT)
    if None in {db_id, trade_id}:
        return None
    payload = f"trade_decline_{db_id}_{trade_id}"
    return payload if len(payload.encode("utf-8")) <= 64 else None


def _trade_cancel_callback(mafile_id, tradeofferid) -> str | None:
    db_id = _bounded_identifier(mafile_id, _MAX_DATABASE_INTEGER)
    trade_id = _bounded_identifier(tradeofferid, _MAX_UNSIGNED_BIGINT)
    if None in {db_id, trade_id}:
        return None
    payload = f"trade_cancel_{db_id}_{trade_id}"
    return payload if len(payload.encode("utf-8")) <= 64 else None


def _inventory_page_callback(
    mafile_id,
    app_id,
    context_id,
    page,
    cursor=None,
) -> str | None:
    db_id = _bounded_identifier(mafile_id, _MAX_DATABASE_INTEGER)
    app = _bounded_identifier(app_id, _MAX_DATABASE_INTEGER)
    context = _bounded_identifier(context_id, _MAX_DATABASE_INTEGER)
    page_value = _bounded_identifier(page, _MAX_DATABASE_INTEGER)
    if None in {db_id, app, context, page_value}:
        return None
    if (app, context) not in {
        ("730", "2"),
        ("570", "2"),
        ("440", "2"),
        ("753", "6"),
    }:
        return None

    suffix = ""
    if cursor == "prev":
        suffix = "_prev"
    elif cursor is not None:
        asset = _bounded_identifier(cursor, _MAX_UNSIGNED_BIGINT)
        if asset is None:
            return None
        suffix = f"_{asset}"

    payload = f"inventory_app_{db_id}_{app}_{context}_{page_value}{suffix}"
    return payload if len(payload.encode("utf-8")) <= 64 else None
