import asyncio
import inspect
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage


USER_ID = 987


def _state() -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=USER_ID, user_id=USER_ID),
    )


class _Session:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def commit(self) -> None:
        self.commits += 1


class _QuerySession(_Session):
    def __init__(self, mafiles) -> None:
        super().__init__()
        self.mafiles = mafiles

    async def execute(self, _statement):
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: self.mafiles)
        )


class _PromptMessage:
    def __init__(self, state: FSMContext) -> None:
        self.state = state
        self.edits = []
        self.states_during_edit = []

    async def edit_text(self, text: str, **kwargs) -> None:
        self.states_during_edit.append(await self.state.get_state())
        self.edits.append((text, kwargs))

    async def answer(self, *_args, **_kwargs) -> None:
        return None


class _Callback:
    def __init__(self, state: FSMContext) -> None:
        self.from_user = SimpleNamespace(id=USER_ID)
        self.data = "trade_notify_toggle_42_on"
        self.message = _PromptMessage(state)
        self._guardbot_acknowledged = True


class _InputMessage:
    def __init__(self, state: FSMContext, text: str, chat_type: str = "private") -> None:
        self.state = state
        self.text = text
        self.chat = SimpleNamespace(type=chat_type)
        self.from_user = SimpleNamespace(id=USER_ID)
        self.answers = []
        self.states_during_answer = []

    async def answer(self, text: str, **kwargs):
        self.states_during_answer.append(await self.state.get_state())
        self.answers.append((text, kwargs))
        return SimpleNamespace()


def test_toggle_callback_starts_typed_interval_flow_before_prompt(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import features
    from guardbot.bot.states import TradeNotificationFlow

    mafile = SimpleNamespace(
        id=42,
        account_name="<b>account</b>",
        trade_notify_interval=5,
    )

    async def owned(*_args, **_kwargs):
        return mafile

    async def feedback(*_args, **_kwargs):
        return None

    monkeypatch.setattr(features, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(features, "get_owned_mafile", owned)
    monkeypatch.setattr(features, "callback_feedback", feedback)

    async def scenario() -> None:
        state = _state()
        await state.update_data(stale=True)
        runtime.user_states[USER_ID] = {
            "state": "waiting_custom_price",
            "mafile_id": 9,
        }
        callback = _Callback(state)

        try:
            await features.callback_trade_notify_toggle(callback, state)

            assert USER_ID not in runtime.user_states
            assert await state.get_state() == TradeNotificationFlow.waiting_interval.state
            assert await state.get_data() == {
                "mafile_id": 42,
                "is_update": False,
            }
            assert callback.message.states_during_edit == [
                TradeNotificationFlow.waiting_interval.state
            ]
            prompt = callback.message.edits[0][0]
            assert "&lt;b&gt;account&lt;/b&gt;" in prompt
            assert "<b>account</b>" not in prompt
        finally:
            runtime.user_states.pop(USER_ID, None)

    asyncio.run(scenario())


def test_interval_callback_starts_update_flow_before_prompt(monkeypatch) -> None:
    from guardbot.bot.handlers import features
    from guardbot.bot.states import TradeNotificationFlow

    mafile = SimpleNamespace(
        id=42,
        account_name="account",
        trade_notify_interval=5,
    )

    async def owned(*_args, **_kwargs):
        return mafile

    async def feedback(*_args, **_kwargs):
        return None

    monkeypatch.setattr(features, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(features, "get_owned_mafile", owned)
    monkeypatch.setattr(features, "callback_feedback", feedback)

    async def scenario() -> None:
        state = _state()
        callback = _Callback(state)
        callback.data = "trade_notify_interval_42"

        await features.callback_trade_notify_interval(callback, state)

        assert await state.get_state() == TradeNotificationFlow.waiting_interval.state
        assert await state.get_data() == {"mafile_id": 42, "is_update": True}
        assert callback.message.states_during_edit == [
            TradeNotificationFlow.waiting_interval.state
        ]

    asyncio.run(scenario())


def test_toggle_off_normalizes_callback_before_redisplaying_menu(monkeypatch) -> None:
    from guardbot.bot.handlers import features

    session = _Session()
    mafile = SimpleNamespace(id=42, trade_notifications=True)
    menu_payloads: list[str] = []

    async def owned(*_args, **_kwargs):
        return mafile

    async def feedback(*_args, **_kwargs):
        return None

    async def menu(callback, _state):
        menu_payloads.append(callback.data)

    monkeypatch.setattr(features, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(features, "get_owned_mafile", owned)
    monkeypatch.setattr(features, "callback_feedback", feedback)
    monkeypatch.setattr(features, "callback_trade_notifications_menu", menu)
    monkeypatch.setattr(features, "stop_trade_check_task", lambda _mafile_id: None)

    async def scenario() -> None:
        state = _state()
        callback = _Callback(state)
        callback.data = "trade_notify_toggle_42_off"
        await features.callback_trade_notify_toggle(callback, state)

    asyncio.run(scenario())

    assert mafile.trade_notifications is False
    assert session.commits == 1
    assert menu_payloads == ["trade_notifications_menu_42"]


def test_interval_input_enables_notifications_and_clears_before_reply(monkeypatch) -> None:
    from guardbot.bot.handlers import features
    from guardbot.bot.states import TradeNotificationFlow

    session = _Session()
    mafile = SimpleNamespace(
        id=42,
        account_name="<b>account</b>",
        trade_notifications=False,
        trade_notify_interval=None,
        known_trade_ids=["old"],
    )
    started = []

    async def owned(*_args, **_kwargs):
        return mafile

    monkeypatch.setattr(features, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(features, "get_owned_mafile", owned)
    monkeypatch.setattr(features, "start_trade_check_task", started.append)

    async def scenario() -> None:
        state = _state()
        await state.set_state(TradeNotificationFlow.waiting_interval)
        await state.set_data({"mafile_id": 42, "is_update": False})
        message = _InputMessage(state, "5")

        await features.process_trade_notification_interval(message, state)

        assert mafile.trade_notifications is True
        assert mafile.trade_notify_interval == 5
        assert mafile.known_trade_ids == []
        assert session.commits == 1
        assert started == [42]
        assert await state.get_state() is None
        assert await state.get_data() == {}
        assert message.states_during_answer == [None]
        assert "&lt;b&gt;account&lt;/b&gt;" in message.answers[0][0]
        assert "<b>account</b>" not in message.answers[0][0]

    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["abc", "0", "61"])
def test_invalid_interval_keeps_flow_for_retry(monkeypatch, text: str) -> None:
    from guardbot.bot.handlers import features
    from guardbot.bot.states import TradeNotificationFlow

    monkeypatch.setattr(
        features,
        "AsyncSessionLocal",
        lambda: pytest.fail("invalid input must not access the database"),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(TradeNotificationFlow.waiting_interval)
        await state.set_data({"mafile_id": 42, "is_update": False})
        message = _InputMessage(state, text)

        await features.process_trade_notification_interval(message, state)

        assert await state.get_state() == TradeNotificationFlow.waiting_interval.state
        assert await state.get_data() == {"mafile_id": 42, "is_update": False}
        assert len(message.answers) == 1

    asyncio.run(scenario())


def test_notify_command_without_interval_starts_typed_flow(monkeypatch) -> None:
    from guardbot.bot.handlers import commands
    from guardbot.bot.states import TradeNotificationFlow

    mafile = SimpleNamespace(id=42, account_name="account")
    monkeypatch.setattr(
        commands,
        "AsyncSessionLocal",
        lambda: _QuerySession([mafile]),
    )

    async def scenario() -> None:
        state = _state()
        message = _InputMessage(state, "/notify_on account")
        command = SimpleNamespace(args="account")

        await commands.cmd_notify_on(message, command, state)

        assert await state.get_state() == TradeNotificationFlow.waiting_interval.state
        assert await state.get_data() == {"mafile_id": 42, "is_update": False}
        assert message.states_during_answer == [TradeNotificationFlow.waiting_interval.state]

    asyncio.run(scenario())


def test_trade_interval_no_longer_uses_legacy_dispatch() -> None:
    from guardbot.bot.handlers import commands, features, messages

    sources = "".join(
        inspect.getsource(module) for module in (commands, features, messages)
    )
    assert '"waiting_trade_notify_interval"' not in sources


def test_notifications_menu_cancels_typed_interval_before_render(monkeypatch) -> None:
    from guardbot.bot.handlers import features
    from guardbot.bot.states import TradeNotificationFlow

    mafile = SimpleNamespace(
        id=42,
        account_name="account",
        trade_notifications=False,
        trade_notify_interval=None,
    )

    async def owned(*_args, **_kwargs):
        return mafile

    async def feedback(*_args, **_kwargs):
        return None

    monkeypatch.setattr(features, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(features, "get_owned_mafile", owned)
    monkeypatch.setattr(features, "callback_feedback", feedback)

    async def scenario() -> None:
        state = _state()
        await state.set_state(TradeNotificationFlow.waiting_interval)
        await state.set_data({"mafile_id": 42, "is_update": False})
        callback = _Callback(state)
        callback.data = "trade_notifications_menu_42"

        await features.callback_trade_notifications_menu(callback, state)

        assert await state.get_state() is None
        assert await state.get_data() == {}
        assert callback.message.states_during_edit == [None]

    asyncio.run(scenario())


def test_interval_input_from_group_preserves_private_flow(monkeypatch) -> None:
    from guardbot.bot.handlers import features
    from guardbot.bot.states import TradeNotificationFlow

    monkeypatch.setattr(
        features,
        "AsyncSessionLocal",
        lambda: pytest.fail("cross-chat input must not access the database"),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(TradeNotificationFlow.waiting_interval)
        await state.set_data({"mafile_id": 42, "is_update": False})
        message = _InputMessage(state, "5", chat_type="group")

        await features.process_trade_notification_interval(message, state)

        assert await state.get_state() == TradeNotificationFlow.waiting_interval.state
        assert await state.get_data() == {"mafile_id": 42, "is_update": False}
        assert "личном чате" in message.answers[0][0]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("handler_name", "callback_data"),
    [
        ("callback_trade_notify_toggle", "trade_notify_toggle_"),
        ("callback_trade_notify_interval", "trade_notify_interval_invalid"),
        ("callback_trade_notifications_menu", "trade_notifications_menu_42_extra"),
        ("callback_trade_notifications_menu", "trade_notifications_menu_-42"),
        ("callback_trade_notify_toggle", "trade_notify_toggle_42_on_extra"),
        ("callback_trade_notify_toggle", "trade_notify_toggle_-42_on"),
        ("callback_trade_notify_interval", "trade_notify_interval_42_extra"),
        ("callback_trade_notify_interval", "trade_notify_interval_-42"),
    ],
)
def test_malformed_interval_callbacks_clear_before_parsing(
    monkeypatch,
    handler_name: str,
    callback_data: str,
) -> None:
    from guardbot.bot.handlers import features
    from guardbot.bot.states import TradeNotificationFlow

    feedbacks = []

    async def feedback(*args, **kwargs):
        feedbacks.append((args, kwargs))

    monkeypatch.setattr(features, "callback_feedback", feedback)
    monkeypatch.setattr(
        features,
        "AsyncSessionLocal",
        lambda: pytest.fail("malformed callback must not access the database"),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(TradeNotificationFlow.waiting_interval)
        await state.set_data({"mafile_id": 42, "is_update": False})
        callback = _Callback(state)
        callback.data = callback_data

        handler = getattr(features, handler_name)
        await handler(callback, state)

        assert await state.get_state() is None
        assert await state.get_data() == {}
        assert len(feedbacks) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "state_data",
    [
        {"mafile_id": 42},
        {"mafile_id": 42, "is_update": "false"},
        {"mafile_id": True, "is_update": False},
        {"mafile_id": 0, "is_update": False},
    ],
)
def test_malformed_interval_state_clears_before_database(
    monkeypatch,
    state_data: dict,
) -> None:
    from guardbot.bot.handlers import features
    from guardbot.bot.states import TradeNotificationFlow

    monkeypatch.setattr(
        features,
        "AsyncSessionLocal",
        lambda: pytest.fail("malformed FSM data must not access the database"),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(TradeNotificationFlow.waiting_interval)
        await state.set_data(state_data)
        message = _InputMessage(state, "5")

        await features.process_trade_notification_interval(message, state)

        assert await state.get_state() is None
        assert await state.get_data() == {}
        assert len(message.answers) == 1

    asyncio.run(scenario())


def test_dispatcher_rejects_cross_chat_interval_input(monkeypatch) -> None:
    from datetime import UTC, datetime

    from aiogram import Bot
    from aiogram.methods import SendMessage
    from aiogram.types import Chat, Message, Update, User

    from guardbot.bot import runtime
    from guardbot.bot.handlers import features
    from guardbot.bot.states import TradeNotificationFlow

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return None

    class LockSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            return Result()

    class FakeBot(Bot):
        def __init__(self) -> None:
            super().__init__(token="42:TEST")
            self.calls = []

        async def __call__(self, method, request_timeout=None):
            self.calls.append(method)
            if isinstance(method, SendMessage):
                return Message(
                    message_id=100 + len(self.calls),
                    date=datetime.now(UTC),
                    chat=Chat(id=int(method.chat_id), type="group"),
                    from_user=User(
                        id=self.id,
                        is_bot=True,
                        first_name="GuardBot",
                    ),
                    text=method.text,
                )
            raise AssertionError(f"Unexpected Bot API method: {method!r}")

    database_calls = []

    def forbidden_feature_session():
        database_calls.append(True)
        raise AssertionError("cross-chat interval must not access account storage")

    monkeypatch.setattr(runtime, "AsyncSessionLocal", LockSession)
    monkeypatch.setattr(features, "AsyncSessionLocal", forbidden_feature_session)

    async def scenario() -> None:
        bot = FakeBot()
        state = runtime.dp.fsm.get_context(bot=bot, chat_id=USER_ID, user_id=USER_ID)
        try:
            await state.set_state(TradeNotificationFlow.waiting_interval)
            await state.set_data({"mafile_id": 42, "is_update": False})

            await runtime.dp.feed_update(
                bot,
                Update(
                    update_id=1,
                    message=Message(
                        message_id=1,
                        date=datetime.now(UTC),
                        chat=Chat(id=-100123, type="group"),
                        from_user=User(
                            id=USER_ID,
                            is_bot=False,
                            first_name="Test",
                        ),
                        text="5",
                    ),
                ),
            )

            assert await state.get_state() == TradeNotificationFlow.waiting_interval.state
            assert await state.get_data() == {"mafile_id": 42, "is_update": False}
            assert database_calls == []
            assert len(bot.calls) == 1
            assert "личном чате" in bot.calls[0].text
        finally:
            await state.clear()
            await bot.session.close()

    asyncio.run(scenario())
