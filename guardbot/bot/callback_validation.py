"""Strict validation helpers for legacy string callback payloads."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from aiogram.types import CallbackQuery

_MAX_DATABASE_INTEGER = 2_147_483_647
_MAX_UNSIGNED_BIGINT = 18_446_744_073_709_551_615

CallbackHandler = Callable[..., Awaitable[Any]]


def validated_callback_data(
    pattern: str,
    *,
    clear_workflows_on_reject: bool = False,
) -> Callable[[CallbackHandler], CallbackHandler]:
    """Reject malformed legacy callback strings before handler side effects.

    Named groups beginning with ``db_id`` are checked against PostgreSQL INTEGER,
    groups beginning with ``int32`` are checked as non-negative signed integers,
    and groups beginning with ``uint64`` are checked as positive unsigned 64-bit
    values. The regular expression must still define the exact callback schema.
    """

    compiled = re.compile(pattern, flags=re.ASCII)

    def decorate(handler: CallbackHandler) -> CallbackHandler:
        @wraps(handler)
        async def wrapped(callback: CallbackQuery, *args: Any, **kwargs: Any) -> Any:
            raw_data = callback.data or ""
            match = compiled.fullmatch(raw_data)
            if (
                len(raw_data.encode("utf-8")) > 64
                or match is None
                or not _named_bounds_are_valid(match.groupdict())
            ):
                if clear_workflows_on_reject:
                    state = kwargs.get("state") or (args[0] if args else None)
                    if state is not None:
                        clear_workflows = handler.__globals__.get(
                            "clear_user_workflows"
                        )
                        if clear_workflows is not None:
                            await clear_workflows(callback.from_user.id, state)
                feedback = handler.__globals__.get("callback_feedback")
                if feedback is None:
                    from guardbot.bot.runtime import callback_feedback as feedback
                await feedback(callback, "❌ Некорректное действие")
                return None
            return await handler(callback, *args, **kwargs)

        return wrapped

    return decorate


def _named_bounds_are_valid(groups: dict[str, str | None]) -> bool:
    for name, raw_value in groups.items():
        if raw_value is None:
            continue
        if name.startswith("db_id"):
            value = int(raw_value)
            if not 0 < value <= _MAX_DATABASE_INTEGER:
                return False
        elif name.startswith("int32"):
            value = int(raw_value)
            if not 0 <= value <= _MAX_DATABASE_INTEGER:
                return False
        elif name.startswith("uint64"):
            value = int(raw_value)
            if not 0 < value <= _MAX_UNSIGNED_BIGINT:
                return False
    return True
