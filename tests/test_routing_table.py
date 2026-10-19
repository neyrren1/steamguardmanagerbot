"""Message routing must not depend on the launcher or on handler file order."""

import json
import os
import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "routing_probe.py"
CATCH_ALL = "handle_messages"


@cache
def _table(launcher: str) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [sys.executable, "-B", str(PROBE), launcher],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def _is_command_sample(label: str) -> bool:
    if label.startswith("text:"):
        return label.removeprefix("text:").startswith("/")
    if label.startswith("document:"):
        caption = label.split("|", 1)[1]
        return caption.startswith("/")
    return False


def test_both_launchers_route_every_message_identically() -> None:
    assert _table("main") == _table("tgbot")


@pytest.mark.parametrize("launcher", ["main", "tgbot"])
def test_commands_reach_the_same_handler_in_every_input_flow(launcher) -> None:
    table = _table(launcher)
    stateless = table["None"]
    mismatches = {
        (state, label): (row[label], stateless[label])
        for state, row in table.items()
        for label in row
        if _is_command_sample(label) and row[label] != stateless[label]
    }
    assert mismatches == {}


@pytest.mark.parametrize("launcher", ["main", "tgbot"])
def test_unknown_and_foreign_commands_only_reach_the_catch_all(launcher) -> None:
    stateless = _table(launcher)["None"]
    assert stateless["text:/unknowncmd"] == CATCH_ALL
    assert stateless["text:/accounts@OtherBot"] == CATCH_ALL
    assert stateless["document:a.zip|/unknowncmd"] == CATCH_ALL
    assert stateless["text:/accounts@TestBot"] == stateless["text:/accounts"]
    assert stateless["text:/accounts"] != CATCH_ALL


@pytest.mark.parametrize("launcher", ["main", "tgbot"])
def test_every_input_flow_consumes_its_own_plain_text(launcher) -> None:
    table = _table(launcher)
    for state, row in table.items():
        if state == "None":
            continue
        assert row["text:hello"] != CATCH_ALL, state


@pytest.mark.parametrize(
    ("file_name", "caption"),
    [("accounts.zip", "/unknowncmd"), ("accounts.txt", "/accounts@OtherBot")],
)
def test_catch_all_never_imports_a_document_sent_as_a_command(
    monkeypatch, file_name, caption
) -> None:
    import asyncio
    from types import SimpleNamespace

    from guardbot.bot.handlers import messages

    async def forbidden(*args, **kwargs):
        raise AssertionError("command-captioned document must not be imported")

    monkeypatch.setattr(messages, "process_zip_archive", forbidden)
    monkeypatch.setattr(messages, "process_txt_accounts", forbidden)

    class Message:
        from_user = SimpleNamespace(id=987)
        text = None
        document = SimpleNamespace(file_name=file_name)

        def __init__(self) -> None:
            self.caption = caption
            self.answers = []

        async def answer(self, text, **kwargs) -> None:
            self.answers.append(text)

    message = Message()
    asyncio.run(messages.handle_messages(message))
    assert message.answers == []


def test_probe_follows_aiogram_router_propagation() -> None:
    """The probe must pick the same handler as a real Dispatcher would."""
    import asyncio
    import importlib.util

    from aiogram import Dispatcher, F, Router

    spec = importlib.util.spec_from_file_location("routing_probe", PROBE)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)

    dispatcher = Dispatcher()
    gated = Router(name="gated")
    gated.message.filter(F.text == "never")
    nested = Router(name="nested")
    after = Router(name="after")

    @dispatcher.message(F.text == "root")
    async def root_handler(message) -> None: ...

    @gated.message()
    async def gated_handler(message) -> None: ...

    @nested.message()
    async def nested_handler(message) -> None: ...

    @after.message()
    async def after_handler(message) -> None: ...

    gated.include_router(nested)
    dispatcher.include_routers(gated, after)

    async def scenario() -> tuple:
        bot = probe._Bot()
        return (
            await probe.first_match(
                dispatcher, "message", probe._message(text="root"), raw_state=None, bot=bot
            ),
            await probe.first_match(
                dispatcher, "message", probe._message(text="other"), raw_state=None, bot=bot
            ),
            await probe.first_match(
                dispatcher, "message", probe._message(text="never"), raw_state=None, bot=bot
            ),
        )

    # A failing root filter skips the router and everything nested in it.
    assert asyncio.run(scenario()) == ("root_handler", "after_handler", "gated_handler")
