import asyncio
import base64
import subprocess
import sys
from pathlib import Path

from guardbot import database, security
from guardbot import __main__ as entrypoint
from guardbot.bot import runtime


ROOT = Path(__file__).resolve().parents[1]


def test_main_preserves_initialization_order(monkeypatch) -> None:
    calls: list[object] = []
    fake_bot = object()

    monkeypatch.setattr(security, "init_fernet", lambda: calls.append("security"))
    monkeypatch.setattr(database, "init_engine", lambda: calls.append("engine"))
    monkeypatch.setattr(runtime, "init_bot", lambda: calls.append("bot"))
    monkeypatch.setattr(runtime.bot, "get", lambda: fake_bot)

    async def init_db() -> None:
        calls.append("database")

    async def init_trade_tasks() -> None:
        calls.append("trade_tasks")

    async def start_polling(bot) -> None:
        calls.append(("polling", bot))

    monkeypatch.setattr(database, "init_db", init_db)
    monkeypatch.setattr(runtime, "init_trade_check_tasks", init_trade_tasks)
    monkeypatch.setattr(runtime.dp, "start_polling", start_polling)

    asyncio.run(entrypoint.main())

    assert calls == [
        "security",
        "engine",
        "bot",
        "database",
        "trade_tasks",
        ("polling", fake_bot),
    ]


def test_generate_key_cli_returns_a_valid_fernet_key() -> None:
    result = subprocess.run(
        [sys.executable, "-B", "tgbot.py", "--generate-key"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    encoded = result.stdout.strip()
    assert len(base64.urlsafe_b64decode(encoded)) == 32
    assert result.stderr == ""
