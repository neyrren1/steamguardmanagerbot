import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage


def _state(user_id: int = 987) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=42, chat_id=user_id, user_id=user_id),
    )


def test_lock_middleware_allows_typed_unlock_password_flow() -> None:
    from guardbot.bot import runtime
    from guardbot.bot.states import UnlockFlow

    async def scenario() -> None:
        state = _state()
        await state.set_state(UnlockFlow.waiting_password)
        calls = []

        async def handler(event, data):
            calls.append(True)
            return "handled"

        event = SimpleNamespace(
            message=SimpleNamespace(
                from_user=SimpleNamespace(id=987),
                chat=SimpleNamespace(type="private"),
                text="secret",
            ),
            callback_query=None,
        )
        data = {
            "state": state,
            "raw_state": await state.get_state(),
        }

        result = await runtime.LockCheckMiddleware()(handler, event, data)

        assert result == "handled"
        assert calls == [True]
        assert await state.get_state() == UnlockFlow.waiting_password.state

    asyncio.run(scenario())


def test_unlock_state_does_not_bypass_lock_for_commands(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.states import UnlockFlow

    locked_user = SimpleNamespace(
        bot_password="stored-hash",
        last_activity=None,
        lock_timeout=None,
    )

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return locked_user

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            return Result()

    class Message:
        from_user = SimpleNamespace(id=987)
        chat = SimpleNamespace(type="private")
        text = "/unlockanything"

        def __init__(self) -> None:
            self.answers = []

        async def answer(self, text, **kwargs) -> None:
            self.answers.append((text, kwargs))

    monkeypatch.setattr(runtime, "AsyncSessionLocal", lambda: Session())

    async def scenario() -> None:
        state = _state()
        await state.set_state(UnlockFlow.waiting_password)
        message = Message()
        calls = []

        async def handler(event, data):
            calls.append(True)

        event = SimpleNamespace(
            message=message,
            callback_query=None,
            inline_query=None,
        )
        data = {"state": state, "raw_state": await state.get_state()}

        await runtime.LockCheckMiddleware()(handler, event, data)

        assert calls == []
        assert "БОТ ЗАБЛОКИРОВАН" in message.answers[0][0]

    asyncio.run(scenario())


def test_locked_inline_query_is_not_dispatched(monkeypatch) -> None:
    from guardbot.bot import runtime

    locked_user = SimpleNamespace(
        bot_password="stored-hash",
        last_activity=None,
        lock_timeout=None,
    )

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return locked_user

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            return Result()

    class InlineQuery:
        from_user = SimpleNamespace(id=987)

        def __init__(self) -> None:
            self.answers = []

        async def answer(self, results, **kwargs) -> None:
            self.answers.append((results, kwargs))

    monkeypatch.setattr(runtime, "AsyncSessionLocal", lambda: Session())

    async def scenario() -> None:
        query = InlineQuery()
        calls = []

        async def handler(event, data):
            calls.append(True)

        event = SimpleNamespace(
            message=None,
            callback_query=None,
            inline_query=query,
        )

        await runtime.LockCheckMiddleware()(handler, event, {})

        assert calls == []
        assert query.answers == [([], {"cache_time": 0, "is_personal": True})]

    asyncio.run(scenario())


def test_automatic_lock_clears_typed_and_legacy_state(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.states import UnlockFlow

    user = SimpleNamespace(
        bot_password="stored-hash",
        last_activity=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=10),
        lock_timeout=1,
    )

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return user

    class Session:
        commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            return Result()

        async def commit(self) -> None:
            self.commits += 1

    session = Session()

    class Message:
        from_user = SimpleNamespace(id=987)
        chat = SimpleNamespace(type="private")
        text = "regular input"

        def __init__(self) -> None:
            self.answers = []

        async def answer(self, text, **kwargs) -> None:
            self.answers.append((text, kwargs))

    monkeypatch.setattr(runtime, "AsyncSessionLocal", lambda: session)

    async def scenario() -> None:
        state = _state()
        await state.set_state(UnlockFlow.waiting_password)
        runtime.user_states[987] = {"state": "waiting_note"}
        message = Message()

        async def handler(event, data):
            raise AssertionError("timed-out update must not be dispatched")

        event = SimpleNamespace(
            message=message,
            callback_query=None,
            inline_query=None,
        )
        data = {"state": state, "raw_state": await state.get_state()}

        await runtime.LockCheckMiddleware()(handler, event, data)

        assert user.last_activity is None
        assert session.commits == 1
        assert await state.get_state() is None
        assert 987 not in runtime.user_states
        assert "БОТ ЗАБЛОКИРОВАН" in message.answers[0][0]

    asyncio.run(scenario())


def test_unlock_command_starts_typed_flow_before_prompt() -> None:
    from guardbot.bot.handlers import commands
    from guardbot.bot.states import UnlockFlow

    state = _state()

    class Message:
        from_user = SimpleNamespace(id=987)
        chat = SimpleNamespace(type="private")

        async def delete(self) -> None:
            return None

        async def answer(self, text, **kwargs) -> None:
            assert await state.get_state() == UnlockFlow.waiting_password.state
            assert 987 not in commands.user_states

    async def scenario() -> None:
        commands.user_states[987] = {"state": "waiting_group_name"}
        await commands.cmd_unlock(Message(), SimpleNamespace(args=None), state)
        assert await state.get_state() == UnlockFlow.waiting_password.state

    asyncio.run(scenario())


def test_unlock_button_starts_typed_flow_before_prompt() -> None:
    from guardbot.bot.handlers import features
    from guardbot.bot.states import UnlockFlow

    state = _state()

    class Message:
        chat = SimpleNamespace(type="private")

        async def edit_text(self, text, **kwargs) -> None:
            assert await state.get_state() == UnlockFlow.waiting_password.state
            assert 987 not in features.user_states

    class Callback:
        from_user = SimpleNamespace(id=987)
        message = Message()

        async def answer(self) -> None:
            return None

    async def scenario() -> None:
        features.user_states[987] = {"state": "waiting_group_name"}
        await features.callback_unlock_bot(Callback(), state)
        assert await state.get_state() == UnlockFlow.waiting_password.state

    asyncio.run(scenario())


def test_wrong_unlock_password_keeps_typed_flow_active(monkeypatch) -> None:
    from guardbot.bot.handlers import commands
    from guardbot.bot.states import UnlockFlow

    user = SimpleNamespace(bot_password="stored-hash")

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return user

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            return Result()

    class Message:
        from_user = SimpleNamespace(id=987)
        chat = SimpleNamespace(type="private")
        text = "wrong-password"

        def __init__(self) -> None:
            self.deleted = False
            self.answers = []

        async def delete(self) -> None:
            self.deleted = True

        async def answer(self, text, **kwargs) -> None:
            self.answers.append((text, kwargs))

    async def invalid_password(*args, **kwargs):
        return False

    monkeypatch.setattr(commands, "AsyncSessionLocal", lambda: Session())
    monkeypatch.setattr(commands, "verify_bot_password_async", invalid_password)

    async def scenario() -> None:
        state = _state()
        await state.set_state(UnlockFlow.waiting_password)
        message = Message()

        await commands.receive_unlock_password(message, state)

        assert message.deleted is True
        assert message.answers == [
            ("❌ <b>Неверный пароль!</b>", {"parse_mode": "HTML"})
        ]
        assert await state.get_state() == UnlockFlow.waiting_password.state
        assert await state.get_data() == {}

    asyncio.run(scenario())


def test_legacy_unlock_state_name_is_removed() -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import commands, features, messages

    for module in (runtime, commands, features, messages):
        assert "waiting_unlock_password" not in inspect.getsource(module)


def test_lock_button_cancels_typed_unlock_flow(monkeypatch) -> None:
    from guardbot.bot.handlers import features
    from guardbot.bot.states import UnlockFlow

    user = SimpleNamespace(bot_password="stored-hash", last_activity="active")

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return user

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            return Result()

        async def commit(self) -> None:
            return None

    class Message:
        async def edit_text(self, text, **kwargs) -> None:
            return None

    class Callback:
        from_user = SimpleNamespace(id=987)
        message = Message()

        async def answer(self, *args, **kwargs) -> None:
            return None

    monkeypatch.setattr(features, "AsyncSessionLocal", lambda: Session())

    async def scenario() -> None:
        state = _state()
        await state.set_state(UnlockFlow.waiting_password)
        features.user_states[987] = {"state": "waiting_note"}

        await features.callback_lock_bot(Callback(), state)

        assert await state.get_state() is None
        assert 987 not in features.user_states

    asyncio.run(scenario())


def test_unlock_cancel_returns_to_locked_screen_without_account_query() -> None:
    from guardbot.bot.handlers import commands
    from guardbot.bot.states import UnlockFlow

    class Message:
        def __init__(self) -> None:
            self.edits = []

        async def edit_text(self, text, **kwargs) -> None:
            self.edits.append((text, kwargs))

    class Callback:
        from_user = SimpleNamespace(id=987)

        def __init__(self) -> None:
            self.message = Message()
            self.answers = 0

        async def answer(self) -> None:
            self.answers += 1

    async def scenario() -> None:
        state = _state()
        await state.set_state(UnlockFlow.waiting_password)
        callback = Callback()

        await commands.callback_cancel_unlock(callback, state)

        assert await state.get_state() is None
        assert "БОТ ЗАБЛОКИРОВАН" in callback.message.edits[0][0]
        assert callback.answers == 1

    asyncio.run(scenario())


def test_unlock_password_is_rejected_outside_private_chat(monkeypatch) -> None:
    from guardbot.bot.handlers import commands
    from guardbot.bot.states import UnlockFlow

    class Message:
        from_user = SimpleNamespace(id=987)
        chat = SimpleNamespace(type="group")
        text = "secret"

        def __init__(self) -> None:
            self.deleted = False
            self.answers = []

        async def delete(self) -> None:
            self.deleted = True

        async def answer(self, text, **kwargs) -> None:
            self.answers.append((text, kwargs))

    monkeypatch.setattr(
        commands,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not be opened")),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(UnlockFlow.waiting_password)
        message = Message()

        await commands.receive_unlock_password(message, state)

        assert message.deleted is True
        assert "личном чате" in message.answers[0][0]
        assert await state.get_state() == UnlockFlow.waiting_password.state

    asyncio.run(scenario())


def test_failed_lock_command_preserves_existing_workflow(monkeypatch) -> None:
    from guardbot.bot.handlers import commands
    from guardbot.bot.states import AccountAddFlow

    user = SimpleNamespace(bot_password=None)

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return user

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            return Result()

    class Message:
        from_user = SimpleNamespace(id=987)

        async def answer(self, text, **kwargs) -> None:
            return None

    monkeypatch.setattr(commands, "AsyncSessionLocal", lambda: Session())

    async def scenario() -> None:
        state = _state()
        await state.set_state(AccountAddFlow.waiting_credentials)
        commands.user_states[987] = {"state": "waiting_note"}

        await commands.cmd_lock(Message(), state)

        assert await state.get_state() == AccountAddFlow.waiting_credentials.state
        assert commands.user_states[987] == {"state": "waiting_note"}

    asyncio.run(scenario())


def test_failed_lock_callback_preserves_existing_workflow(monkeypatch) -> None:
    from guardbot.bot.handlers import features
    from guardbot.bot.states import AccountAddFlow

    user = SimpleNamespace(bot_password=None)

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return user

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            return Result()

    class Callback:
        from_user = SimpleNamespace(id=987)

        async def answer(self, text=None, **kwargs) -> None:
            return None

    monkeypatch.setattr(features, "AsyncSessionLocal", lambda: Session())

    async def scenario() -> None:
        state = _state()
        await state.set_state(AccountAddFlow.waiting_credentials)
        features.user_states[987] = {"state": "waiting_note"}

        await features.callback_lock_bot(Callback(), state)

        assert await state.get_state() == AccountAddFlow.waiting_credentials.state
        assert features.user_states[987] == {"state": "waiting_note"}

    asyncio.run(scenario())
