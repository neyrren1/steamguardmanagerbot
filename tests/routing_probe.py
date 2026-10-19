"""Print which message handler wins for every (FSM state, message) pair.

Run in a fresh interpreter so the launcher's own import order builds the
registry: ``python -B tests/routing_probe.py main|tgbot``. Only handler filters
are evaluated (middleware is not), which is exactly the part that depends on
registration order.

Not modelled, because the bot uses neither: handlers raising ``SkipHandler``
and per-router outer middlewares. Extend ``first_match`` before adding them.
"""

import asyncio
import importlib
import json
import sys
from datetime import datetime

from aiogram.fsm.state import StatesGroup
from aiogram.types import Chat, Document, Message, PhotoSize, User

BOT_USERNAME = "TestBot"

TEXTS = (
    "hello",
    "123456",
    "/my_proxy",
    "/accounts",
    "/bulk_import",
    "/import_mafile",
    "/login acc",
    "/note",
    "/gc",
    "/start",
    "/cancel",
    "/unknowncmd",
    "/accounts@OtherBot",
    "/accounts@TestBot",
)
DOCUMENTS = (
    ("a.json", None),
    ("a.zip", None),
    ("a.txt", None),
    ("a.json", "hello"),
    ("a.zip", "/my_proxy"),
    ("a.zip", "/unknowncmd"),
)


class _Bot:
    id = 42

    async def me(self) -> User:
        return User(id=42, is_bot=True, first_name="bot", username=BOT_USERNAME)


def _message(*, text=None, document=None, caption=None, photo=False) -> Message:
    return Message(
        message_id=1,
        date=datetime(2026, 1, 1),
        chat=Chat(id=987, type="private"),
        from_user=User(id=987, is_bot=False, first_name="user"),
        text=text,
        caption=caption,
        document=(
            Document(file_id="file", file_unique_id="unique", file_name=document)
            if document
            else None
        ),
        photo=[PhotoSize(file_id="p", file_unique_id="pu", width=1, height=1)] if photo else None,
    )


def _samples():
    for text in TEXTS:
        yield f"text:{text}", _message(text=text)
    for name, caption in DOCUMENTS:
        yield f"document:{name}|{caption}", _message(document=name, caption=caption)
    yield "photo", _message(photo=True)


def _all_states():
    from guardbot.bot import states

    found = []
    for value in vars(states).values():
        if isinstance(value, type) and issubclass(value, StatesGroup) and value is not StatesGroup:
            found.extend(state.state for state in value.__all_states__)
    return sorted(set(found))


def handler_name(handler) -> str:
    callback = handler.callback
    callback = getattr(callback, "__wrapped__", callback)
    return callback.__name__


async def first_match(router, event_name, event, **kwargs):
    """Name the handler aiogram would run, mirroring ``Router._propagate_event``.

    A router first checks its root filters (``router.<event>.filter(...)``); if
    they fail, neither its handlers nor any of its nested routers are tried.
    Otherwise its own handlers are tried in order, then each nested router.
    """
    observer = router.observers[event_name]
    passed, data = await observer.check_root_filters(event, **kwargs)
    if not passed:
        return None
    kwargs = {**kwargs, **data}
    for handler in observer.handlers:
        matched, _ = await handler.check(event, **kwargs)
        if matched:
            return handler_name(handler)
    for sub_router in router.sub_routers:
        found = await first_match(sub_router, event_name, event, **kwargs)
        if found is not None:
            return found
    return None


async def build_table(dispatcher) -> dict:
    bot = _Bot()
    table = {}
    for raw_state in [None, *_all_states()]:
        row = {}
        for label, message in _samples():
            row[label] = await first_match(
                dispatcher, "message", message, raw_state=raw_state, bot=bot
            )
        table[str(raw_state)] = row
    return table


def load_launcher(name: str):
    if name == "main":
        importlib.import_module("guardbot.__main__")
    elif name == "tgbot":
        sys.argv = ["tgbot.py"]
        importlib.import_module("tgbot")
    else:
        raise SystemExit(f"unknown launcher {name!r}")
    from guardbot.bot import routing, runtime

    # What guardbot.__main__.main() does before polling.
    routing.setup_routers(runtime.dp)
    return runtime.dp


def main() -> None:
    dispatcher = load_launcher(sys.argv[1])
    print(json.dumps(asyncio.run(build_table(dispatcher)), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
