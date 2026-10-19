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


class _ChallengeClient:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


async def _publish_challenge(user_id: int, state, client) -> str:
    from guardbot.bot import pending_challenges
    from guardbot.bot.states import SteamChallengeFlow

    return await pending_challenges.publish_pending_challenge(
        user_id=user_id,
        mafile_id=42,
        kind="email",
        client=client,
        state=state,
        fsm_state=SteamChallengeFlow.waiting_code,
        ttl_seconds=60,
    )


class PromptMessage:
    chat = SimpleNamespace(type="private")

    def __init__(self, state: FSMContext, error: BaseException | None = None) -> None:
        self.state = state
        self.error = error
        self.snapshots = []

    async def edit_text(self, *_args, **_kwargs) -> None:
        self.snapshots.append(
            (await self.state.get_state(), await self.state.get_data())
        )
        if self.error is not None:
            raise self.error


class Callback:
    def __init__(self, state: FSMContext, error: BaseException | None = None) -> None:
        self.from_user = SimpleNamespace(id=USER_ID)
        self.message = PromptMessage(state, error)
        self._guardbot_acknowledged = True


@pytest.mark.parametrize(
    ("handler_name", "action"),
    [
        ("callback_set_password", "set"),
        ("callback_change_password", "change"),
        ("callback_remove_password", "remove"),
    ],
)
def test_password_callbacks_start_typed_flow_before_prompt(
    monkeypatch,
    handler_name: str,
    action: str,
) -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import features
    from guardbot.bot.states import BotPasswordFlow

    async def scenario() -> None:
        state = _state()
        client = _ChallengeClient()
        await _publish_challenge(USER_ID, state, client)
        await state.update_data(stale=True)
        callback = Callback(state)

        try:
            await getattr(features, handler_name)(callback, state)
            assert client.close_calls == 1
            assert pending_challenges.current_pending_challenge_token(USER_ID) is None
            assert await state.get_state() == BotPasswordFlow.waiting_password.state
            assert await state.get_data() == {"action": action}
            assert callback.message.snapshots == [
                (BotPasswordFlow.waiting_password.state, {"action": action})
            ]
        finally:
            await pending_challenges.clear_pending_challenges_for_user(USER_ID, state)

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [RuntimeError("failed"), asyncio.CancelledError()])
def test_password_callback_prompt_failure_rolls_back(
    monkeypatch,
    error: BaseException,
) -> None:
    from guardbot.bot.handlers import features

    async def scenario() -> None:
        state = _state()
        with pytest.raises(type(error)):
            await features.callback_set_password(Callback(state, error), state)
        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(scenario())


class Result:
    def __init__(self, user) -> None:
        self.user = user

    def scalar_one_or_none(self):
        return self.user


class InputMessage:
    chat = SimpleNamespace(type="private")

    def __init__(self, state: FSMContext, text: str) -> None:
        self.state = state
        self.text = text
        self.from_user = SimpleNamespace(
            id=USER_ID,
            username="tester",
            full_name="Tester",
        )
        self.deleted = False
        self.answers = []

    async def delete(self) -> None:
        self.deleted = True

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append(
            (text, kwargs, await self.state.get_state(), await self.state.get_data())
        )


def test_password_input_deletes_before_database_and_clears_after_commit(monkeypatch) -> None:
    from guardbot.bot.handlers import commands
    from guardbot.bot.states import BotPasswordFlow

    user = SimpleNamespace(bot_password="old", last_activity=None)
    holder = {}

    class Session:
        async def __aenter__(self):
            assert holder["message"].deleted is True
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            return Result(user)

        async def commit(self):
            holder["committed"] = True

    async def hash_password(password: str) -> str:
        assert password == "new password"
        return "hashed"

    monkeypatch.setattr(commands, "AsyncSessionLocal", lambda: Session())
    monkeypatch.setattr(commands, "hash_bot_password_async", hash_password)

    async def scenario() -> None:
        state = _state()
        await state.set_state(BotPasswordFlow.waiting_password)
        await state.set_data({"action": "change"})
        message = InputMessage(state, "new password")
        holder["message"] = message

        await commands.receive_bot_password(message, state)

        assert holder["committed"] is True
        assert user.bot_password == "hashed"
        assert await state.get_state() is None
        assert await state.get_data() == {}
        assert message.answers[-1][2:] == (None, {})

    asyncio.run(scenario())


def test_password_deletion_failure_aborts_before_fsm_data_and_database(monkeypatch) -> None:
    from guardbot.bot.handlers import commands
    from guardbot.bot.states import BotPasswordFlow

    async def deletion_failed(_message):
        return False

    monkeypatch.setattr(commands, "delete_secret_message_or_abort", deletion_failed)
    monkeypatch.setattr(
        commands,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not open")),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(BotPasswordFlow.waiting_password)
        await state.set_data({"action": "set"})
        message = InputMessage(state, "very-secret")

        original_get_data = state.get_data

        async def forbidden_get_data():
            raise AssertionError("FSM data must not be read after deletion failure")

        state.get_data = forbidden_get_data
        try:
            await commands.receive_bot_password(message, state)
        finally:
            state.get_data = original_get_data

        assert await state.get_state() == BotPasswordFlow.waiting_password.state

    asyncio.run(scenario())


def test_secret_deletion_marker_prevents_duplicate_telegram_delete() -> None:
    from guardbot.bot.runtime import delete_secret_message_or_abort

    class Message:
        def __init__(self) -> None:
            self.deletes = 0

        async def delete(self) -> None:
            self.deletes += 1

    async def scenario() -> None:
        message = Message()
        assert await delete_secret_message_or_abort(message) is True
        assert await delete_secret_message_or_abort(message) is True
        assert message.deletes == 1

    asyncio.run(scenario())


def test_secret_deletion_runtime_failure_aborts_cleanly() -> None:
    from guardbot.bot.runtime import delete_secret_message_or_abort

    class Message:
        def __init__(self) -> None:
            self.answers = []

        async def delete(self) -> None:
            raise RuntimeError("delete failed")

        async def answer(self, text: str) -> None:
            self.answers.append(text)

    async def scenario() -> None:
        message = Message()

        assert await delete_secret_message_or_abort(message) is False
        assert len(message.answers) == 1
        assert "не удалось удалить" in message.answers[0].lower()

    asyncio.run(scenario())


def test_malformed_password_state_clears_before_database(monkeypatch) -> None:
    from guardbot.bot.handlers import commands
    from guardbot.bot.states import BotPasswordFlow

    monkeypatch.setattr(
        commands,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not open")),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(BotPasswordFlow.waiting_password)
        await state.set_data({"action": True})
        message = InputMessage(state, "very-secret")

        await commands.receive_bot_password(message, state)

        assert message.deleted is True
        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(scenario())


def test_short_password_stays_retryable_without_database(monkeypatch) -> None:
    from guardbot.bot.handlers import commands
    from guardbot.bot.states import BotPasswordFlow

    monkeypatch.setattr(
        commands,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not open")),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(BotPasswordFlow.waiting_password)
        await state.set_data({"action": "set"})
        message = InputMessage(state, "short")

        await commands.receive_bot_password(message, state)

        assert message.deleted is True
        assert await state.get_state() == BotPasswordFlow.waiting_password.state
        assert await state.get_data() == {"action": "set"}

    asyncio.run(scenario())


def test_bot_password_no_longer_uses_legacy_dispatch() -> None:
    from guardbot.bot.handlers import commands, features, messages

    sources = "\n".join(
        [
            inspect.getsource(commands.cmd_set_password),
            inspect.getsource(features._start_bot_password_prompt),
            inspect.getsource(messages.handle_messages),
        ]
    )
    assert '"waiting_bot_password"' not in sources


def test_group_password_text_is_deleted_without_consuming_private_flow(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.states import BotPasswordFlow

    class Message:
        from_user = SimpleNamespace(id=USER_ID)
        chat = SimpleNamespace(type="group")
        text = "very-secret"

        def __init__(self) -> None:
            self.deleted = False
            self.answers = []

        async def delete(self) -> None:
            self.deleted = True

        async def answer(self, text, **kwargs) -> None:
            self.answers.append((text, kwargs))

    monkeypatch.setattr(
        runtime,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not open")),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(BotPasswordFlow.waiting_password)
        await state.set_data({"action": "set"})
        message = Message()
        event = SimpleNamespace(
            message=message,
            callback_query=None,
            inline_query=None,
        )

        async def forbidden_handler(*_args):
            raise AssertionError("group secret must not be dispatched")

        await runtime.LockCheckMiddleware()(
            forbidden_handler,
            event,
            {"state": state, "raw_state": await state.get_state()},
        )

        assert message.deleted is True
        assert "личном чате" in message.answers[0][0]
        assert await state.get_state() == BotPasswordFlow.waiting_password.state
        assert await state.get_data() == {"action": "set"}

    asyncio.run(scenario())
