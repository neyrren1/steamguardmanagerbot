import ast
import asyncio
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiogram.enums import ButtonStyle
from aiogram.types import (
    CallbackQuery,
    Chat,
    DisabledButton,
    InputRichBlockButtons,
    InputRichBlockSectionHeading,
    InputRichMessage,
    Message,
    RichTextButton,
    Update,
    User,
)

from guardbot.bot.rich_demo import (
    RICH_DEMO_CALLBACK_PREFIX,
    build_rich_demo_message,
    get_rich_demo_feedback,
    send_rich_demo,
)

ROOT = Path(__file__).resolve().parents[1]


def _embedded_buttons(rich_message: InputRichMessage) -> list[RichTextButton]:
    buttons: list[RichTextButton] = []
    for block in rich_message.blocks or []:
        text = getattr(block, "text", None)
        values = text if isinstance(text, list) else [text]
        buttons.extend(value for value in values if isinstance(value, RichTextButton))
    return buttons


def test_rich_demo_contains_embedded_and_colored_buttons() -> None:
    rich_message = build_rich_demo_message()

    assert isinstance(rich_message, InputRichMessage)
    assert any(isinstance(block, InputRichBlockSectionHeading) for block in rich_message.blocks)

    embedded = _embedded_buttons(rich_message)
    assert embedded
    assert any(button.button.style == ButtonStyle.SUCCESS for button in embedded)
    assert any(isinstance(button.button.disabled, DisabledButton) for button in embedded)

    button_blocks = [
        block for block in rich_message.blocks if isinstance(block, InputRichBlockButtons)
    ]
    assert button_blocks

    buttons = [button for block in button_blocks for button in block.buttons]
    assert {button.style for button in buttons} >= {
        ButtonStyle.PRIMARY,
        ButtonStyle.SUCCESS,
        ButtonStyle.DANGER,
    }
    assert any(button.url == "https://core.telegram.org/bots/api#richmessagebutton" for button in buttons)
    assert all(len(block.buttons) <= 8 for block in button_blocks)

    url_button = next(button for button in buttons if button.url)
    assert url_button.style is None

    all_buttons = [button.button for button in embedded] + buttons
    for button in all_buttons:
        actions = (button.callback_data, button.url, button.disabled)
        assert sum(action is not None for action in actions) == 1

    callback_buttons = [button for button in buttons if button.callback_data]
    assert callback_buttons
    assert all(
        button.callback_data.startswith(RICH_DEMO_CALLBACK_PREFIX)
        for button in callback_buttons
    )


def test_rich_demo_feedback_rejects_unknown_actions() -> None:
    assert get_rich_demo_feedback("success") == (
        "Зелёная callback-кнопка работает.",
        False,
    )
    assert get_rich_demo_feedback("unknown") is None


def test_send_rich_demo_uses_send_rich_message() -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def send_rich_message(self, **kwargs):
            self.calls.append(kwargs)
            return "sent"

    bot = FakeBot()

    result = asyncio.run(
        send_rich_demo(
            bot,
            chat_id=123456,
            business_connection_id="business-123",
        )
    )

    assert result == "sent"
    assert len(bot.calls) == 1
    assert bot.calls[0]["chat_id"] == 123456
    assert bot.calls[0]["business_connection_id"] == "business-123"
    assert isinstance(bot.calls[0]["rich_message"], InputRichMessage)


def test_rich_demo_command_handler_sends_to_current_chat() -> None:
    from guardbot.bot.handlers.rich_demo import cmd_rich_demo

    class FakeBot:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def send_rich_message(self, **kwargs):
            self.calls.append(kwargs)

    class FakeMessage:
        def __init__(self) -> None:
            self.bot = FakeBot()
            self.from_user = type("User", (), {"id": 654321})()
            self.chat = type("Chat", (), {"id": 654321, "type": "private"})()
            self.message_thread_id = 777
            self.business_connection_id = "business-123"

    message = FakeMessage()

    asyncio.run(cmd_rich_demo(message))

    assert message.bot.calls[0]["chat_id"] == 654321
    assert message.bot.calls[0]["message_thread_id"] == 777
    assert message.bot.calls[0]["business_connection_id"] == "business-123"


def test_rich_demo_command_rejects_group_without_sending_demo() -> None:
    from guardbot.bot.handlers.rich_demo import cmd_rich_demo

    class FakeBot:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def send_rich_message(self, **kwargs) -> None:
            self.calls.append(kwargs)

    class FakeMessage:
        def __init__(self) -> None:
            self.bot = FakeBot()
            self.from_user = type("User", (), {"id": 654321})()
            self.chat = type("Chat", (), {"id": -100123, "type": "group"})()
            self.message_thread_id = None
            self.business_connection_id = None
            self.answers: list[str] = []

        async def answer(self, text: str) -> None:
            self.answers.append(text)

    message = FakeMessage()

    asyncio.run(cmd_rich_demo(message))

    assert message.bot.calls == []
    assert message.answers == [
        "🔒 Демонстрация Rich Message доступна только в личном чате с ботом."
    ]


def test_group_rich_demo_clears_global_typed_and_legacy_workflows(
    monkeypatch,
) -> None:
    from aiogram import Bot
    from aiogram.methods import DeleteMessage, SendMessage
    from aiogram.types import MessageEntity

    from guardbot.bot import runtime
    from guardbot.bot.handlers import rich_demo, messages  # noqa: F401
    from guardbot.bot.states import AccountAddFlow

    user_id = 654_322

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return None

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            return Result()

    class FakeBot(Bot):
        def __init__(self) -> None:
            super().__init__(token="42:TEST")
            self.calls = []

        async def __call__(self, method, request_timeout=None):
            self.calls.append(method)
            if isinstance(method, DeleteMessage):
                return True
            if isinstance(method, SendMessage):
                return Message(
                    message_id=2,
                    date=datetime.now(UTC),
                    chat=Chat(id=-100123, type="group"),
                    text=method.text,
                )
            raise AssertionError(f"Unexpected Bot API method: {method!r}")

    monkeypatch.setattr(runtime, "AsyncSessionLocal", lambda: Session())

    async def scenario() -> None:
        bot = FakeBot()
        state = runtime.dp.fsm.get_context(
            bot=bot,
            chat_id=user_id,
            user_id=user_id,
        )
        await state.set_state(AccountAddFlow.waiting_credentials)
        runtime.user_states[user_id] = {"state": "waiting_steam_code_manual"}
        command = "/rich_demo"
        update = Update(
            update_id=1,
            message=Message(
                message_id=1,
                date=datetime.now(UTC),
                chat=Chat(id=-100123, type="group"),
                from_user=User(id=user_id, is_bot=False, first_name="Test"),
                text=command,
                entities=[
                    MessageEntity(
                        type="bot_command",
                        offset=0,
                        length=len(command),
                    )
                ],
            ),
        )

        try:
            await runtime.dp.feed_update(bot, update)

            assert await state.get_state() is None
            assert user_id not in runtime.user_states
            feedback = [
                call.text for call in bot.calls if isinstance(call, SendMessage)
            ]
            assert feedback == [
                "🔒 Демонстрация Rich Message доступна только в личном чате с ботом."
            ]
        finally:
            await state.clear()
            await runtime.clear_legacy_user_state(user_id)
            await bot.session.close()

    asyncio.run(scenario())


def test_rich_demo_callback_handler_answers_known_action() -> None:
    from guardbot.bot.handlers.rich_demo import callback_rich_demo

    class Message:
        def __init__(self) -> None:
            self.feedback: list[str] = []

        async def answer(self, text: str, **kwargs) -> None:
            self.feedback.append(text)

    class FakeCallback:
        data = "rich_demo:danger"

        def __init__(self) -> None:
            self.answers: list[dict] = []
            self.message = Message()

        async def answer(self, text: str | None = None, **kwargs) -> None:
            self.answers.append(
                {"text": text, "show_alert": kwargs.get("show_alert")}
            )

    callback = FakeCallback()

    asyncio.run(callback_rich_demo(callback))

    assert callback.answers == [{"text": None, "show_alert": None}]
    assert callback.message.feedback == [
        "Красная кнопка работает. Это только демонстрация."
    ]


def test_rich_demo_callback_handler_rejects_unknown_action() -> None:
    from guardbot.bot.handlers.rich_demo import callback_rich_demo

    class Message:
        def __init__(self) -> None:
            self.feedback: list[str] = []

        async def answer(self, text: str, **kwargs) -> None:
            self.feedback.append(text)

    class FakeCallback:
        data = f"{RICH_DEMO_CALLBACK_PREFIX}unknown"

        def __init__(self) -> None:
            self.answers: list[dict] = []
            self.message = Message()

        async def answer(self, text: str | None = None, **kwargs) -> None:
            self.answers.append(
                {"text": text, "show_alert": kwargs.get("show_alert")}
            )

    callback = FakeCallback()

    asyncio.run(callback_rich_demo(callback))

    assert callback.answers == [{"text": None, "show_alert": None}]
    assert callback.message.feedback == ["Неизвестное демонстрационное действие."]


def test_entrypoint_imports_rich_demo_handler() -> None:
    tree = ast.parse((ROOT / "guardbot/__main__.py").read_text(encoding="utf-8"))
    imported_names = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module == "guardbot.bot.handlers"
        for alias in node.names
    ]

    assert "rich_demo" in imported_names
    assert imported_names.index("rich_demo") < imported_names.index("messages")


def test_documented_launcher_registers_rich_demo_before_catch_all() -> None:
    script = """
import tgbot
from guardbot.bot.handlers.messages import handle_messages
from guardbot.bot.handlers.rich_demo import cmd_rich_demo

callbacks = [handler.callback for handler in tgbot.dp.message.handlers]
assert callbacks.index(cmd_rich_demo) < callbacks.index(handle_messages)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_declared_aiogram_version_supports_rich_messages() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert "aiogram>=3.31,<4" in pyproject["project"]["dependencies"]


def test_lock_middleware_checks_database_for_rich_demo(monkeypatch) -> None:
    from guardbot.bot import runtime

    class DatabaseOpened(RuntimeError):
        pass

    def record_database_access():
        raise DatabaseOpened

    monkeypatch.setattr(runtime, "AsyncSessionLocal", record_database_access)

    class FakeBot:
        async def me(self) -> User:
            return User(id=999, is_bot=True, first_name="Demo", username="TestBot")

    user = User(id=123, is_bot=False, first_name="Tester")
    chat = Chat(id=123, type="private")
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=chat,
        from_user=user,
        text="/rich_demo@TestBot",
    )

    events = [
        Update(update_id=1, message=message),
        Update(
            update_id=2,
            callback_query=CallbackQuery(
                id="callback-1",
                from_user=user,
                chat_instance="chat-1",
                data=f"{RICH_DEMO_CALLBACK_PREFIX}success",
                message=message,
            ),
        ),
    ]
    async def handler(event, data):
        raise AssertionError("locked rich_demo must not reach its handler")

    middleware = runtime.LockCheckMiddleware()
    for event in events:
        with pytest.raises(DatabaseOpened):
            asyncio.run(middleware(handler, event, {"bot": FakeBot()}))


def test_lock_middleware_does_not_bypass_other_bot_mention(monkeypatch) -> None:
    from guardbot.bot import runtime

    class DatabaseOpened(RuntimeError):
        pass

    def record_database_access():
        raise DatabaseOpened

    monkeypatch.setattr(runtime, "AsyncSessionLocal", record_database_access)

    class FakeBot:
        async def me(self) -> User:
            return User(id=999, is_bot=True, first_name="Demo", username="TestBot")

    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=123, type="private"),
        from_user=User(id=123, is_bot=False, first_name="Tester"),
        text="/rich_demo@OtherBot",
    )
    event = Update(update_id=1, message=message)

    async def handler(event, data):
        raise AssertionError("mismatched mention must not reach rich_demo")

    with pytest.raises(DatabaseOpened):
        asyncio.run(runtime.LockCheckMiddleware()(handler, event, {"bot": FakeBot()}))
