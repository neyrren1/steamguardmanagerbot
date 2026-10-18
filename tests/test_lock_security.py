import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import DeleteMessage, EditMessageText, SendMessage


def _state(user_id: int = 987) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=42, chat_id=user_id, user_id=user_id),
    )


def test_failed_automatic_lock_preserves_workflow_state(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.states import AccountAddFlow

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
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            return Result()

        async def commit(self) -> None:
            raise RuntimeError("database unavailable")

    class Message:
        from_user = SimpleNamespace(id=987)
        chat = SimpleNamespace(type="private")
        text = "regular input"

    monkeypatch.setattr(runtime, "AsyncSessionLocal", lambda: Session())

    async def scenario() -> None:
        state = _state()
        await state.set_state(AccountAddFlow.waiting_credentials)
        runtime.user_states[987] = {"state": "waiting_note"}
        event = SimpleNamespace(
            message=Message(),
            callback_query=None,
            inline_query=None,
        )
        data = {"state": state, "raw_state": await state.get_state()}

        try:
            with pytest.raises(RuntimeError, match="database unavailable"):
                await runtime.LockCheckMiddleware()(lambda *_: None, event, data)

            assert await state.get_state() == AccountAddFlow.waiting_credentials.state
            assert runtime.user_states[987] == {"state": "waiting_note"}
        finally:
            runtime.user_states.pop(987, None)
            await state.clear()

    asyncio.run(scenario())


def test_blocked_callback_is_acknowledged(monkeypatch) -> None:
    from guardbot.bot import runtime

    user = SimpleNamespace(
        bot_password="stored-hash",
        last_activity=None,
        lock_timeout=None,
    )

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
        chat = SimpleNamespace(type="private")

        async def answer(self, text, **kwargs) -> None:
            return None

    class Callback:
        from_user = SimpleNamespace(id=987)
        data = "account_123"
        message = Message()

        def __init__(self) -> None:
            self.acks = 0

        async def answer(self, *args, **kwargs) -> None:
            self.acks += 1

    monkeypatch.setattr(runtime, "AsyncSessionLocal", lambda: Session())

    async def scenario() -> None:
        callback = Callback()
        event = SimpleNamespace(
            message=None,
            callback_query=callback,
            inline_query=None,
        )

        await runtime.LockCheckMiddleware()(lambda *_: None, event, {})

        assert callback.acks == 1

    asyncio.run(scenario())


def test_short_set_password_message_is_deleted(monkeypatch) -> None:
    from guardbot.bot.handlers import commands

    class Message:
        from_user = SimpleNamespace(id=987, username="tester", full_name="Tester")
        chat = SimpleNamespace(type="private")

        def __init__(self) -> None:
            self.deleted = False

        async def delete(self) -> None:
            self.deleted = True

        async def answer(self, text, **kwargs) -> None:
            return None

    monkeypatch.setattr(
        commands,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not be opened")),
    )

    async def scenario() -> None:
        message = Message()
        await commands.cmd_set_password(
            message,
            SimpleNamespace(args="short"),
            _state(),
        )
        assert message.deleted is True

    asyncio.run(scenario())


def test_first_password_install_keeps_session_unlocked(monkeypatch) -> None:
    from guardbot.bot.handlers import commands
    from guardbot.passwords import MIN_BOT_PASSWORD_LENGTH

    user = SimpleNamespace(bot_password=None, last_activity=None)

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
        from_user = SimpleNamespace(id=987, username="tester", full_name="Tester")
        chat = SimpleNamespace(type="private")

        async def delete(self) -> None:
            return None

        async def answer(self, text, **kwargs) -> None:
            return None

    async def fake_hash(password: str) -> str:
        return "hashed"

    monkeypatch.setattr(commands, "AsyncSessionLocal", lambda: Session())
    monkeypatch.setattr(commands, "hash_bot_password_async", fake_hash)

    async def scenario() -> None:
        password = "x" * MIN_BOT_PASSWORD_LENGTH
        await commands.cmd_set_password(
            Message(),
            SimpleNamespace(args=password),
            _state(),
        )

        assert user.bot_password == "hashed"
        assert user.last_activity is not None

    asyncio.run(scenario())


def test_unlock_command_preserves_multiword_password(monkeypatch) -> None:
    from guardbot.bot.handlers import commands

    user = SimpleNamespace(
        bot_password="stored-hash",
        last_activity=None,
    )
    verified_passwords = []

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return user

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            return Result()

        async def commit(self):
            return None

    class Message:
        from_user = SimpleNamespace(id=987)
        chat = SimpleNamespace(type="private")

        async def delete(self) -> None:
            return None

        async def answer(self, *_args, **_kwargs) -> None:
            return None

    async def verify(_stored, password, **_kwargs):
        verified_passwords.append(password)
        return True

    async def start_help(_message):
        return None

    monkeypatch.setattr(commands, "AsyncSessionLocal", Session)
    monkeypatch.setattr(commands, "verify_bot_password_async", verify)
    monkeypatch.setattr(commands, "password_hash_needs_upgrade", lambda _: False)
    monkeypatch.setattr(commands, "cmd_start_help", start_help)

    async def scenario() -> None:
        state = _state()
        await commands.cmd_unlock(
            Message(),
            SimpleNamespace(args="correct horse battery staple"),
            state,
        )

        assert verified_passwords == ["correct horse battery staple"]
        assert user.last_activity is not None

    asyncio.run(scenario())


def test_group_unlock_text_is_deleted_before_lock_short_circuit(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.states import UnlockFlow

    class Message:
        from_user = SimpleNamespace(id=987)
        chat = SimpleNamespace(type="group")
        text = "/secret-looking-password"

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
        lambda: (_ for _ in ()).throw(AssertionError("database must not be opened")),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(UnlockFlow.waiting_password)
        message = Message()
        event = SimpleNamespace(
            message=message,
            callback_query=None,
            inline_query=None,
        )
        data = {"state": state, "raw_state": await state.get_state()}

        async def handler(event, data):
            raise AssertionError("group password must not be dispatched")

        await runtime.LockCheckMiddleware()(handler, event, data)

        assert message.deleted is True
        assert "личном чате" in message.answers[0][0]
        assert await state.get_state() == UnlockFlow.waiting_password.state

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "legacy_state",
    [
        "waiting_steam_code",
        "waiting_steam_code_or_mafile",
    ],
)
def test_group_legacy_secret_is_rejected_before_database(
    monkeypatch,
    legacy_state,
) -> None:
    from guardbot.bot import runtime

    class Message:
        from_user = SimpleNamespace(id=987)
        chat = SimpleNamespace(type="supergroup")
        text = "secret"

        def __init__(self) -> None:
            self.deleted = False

        async def delete(self) -> None:
            self.deleted = True

        async def answer(self, text, **kwargs) -> None:
            return None

    monkeypatch.setattr(
        runtime,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not be opened")),
    )

    async def scenario() -> None:
        message = Message()
        runtime.user_states[987] = {"state": legacy_state}
        try:
            event = SimpleNamespace(
                message=message,
                callback_query=None,
                inline_query=None,
            )

            async def forbidden_handler(event, data):
                raise AssertionError("secret must not be dispatched")

            await runtime.LockCheckMiddleware()(
                forbidden_handler,
                event,
                {"raw_state": None, "state": _state()},
            )
            assert message.deleted is True
        finally:
            runtime.user_states.pop(987, None)

    asyncio.run(scenario())


def test_secret_callbacks_reject_non_private_chats() -> None:
    from guardbot.bot.handlers import features

    class Message:
        chat = SimpleNamespace(type="group")

        def __init__(self) -> None:
            self.feedback: list[str] = []

        async def answer(self, text, **kwargs) -> None:
            self.feedback.append(text)

    class Callback:
        from_user = SimpleNamespace(id=987)

        def __init__(self) -> None:
            self.answers = []
            self.message = Message()

        async def answer(self, text=None, **kwargs) -> None:
            self.answers.append((text, kwargs))

    async def scenario() -> None:
        state = _state()
        for handler in (
            features.callback_set_password,
            features.callback_change_password,
            features.callback_remove_password,
        ):
            callback = Callback()
            await handler(callback, state)
            assert callback.answers == [(None, {})]
            assert "личном чате" in callback.message.feedback[0]
            assert 987 not in features.user_states

        callback = Callback()
        await features.callback_unlock_bot(callback, state)
        assert callback.answers == [(None, {})]
        assert "личном чате" in callback.message.feedback[0]
        assert await state.get_state() is None

    asyncio.run(scenario())


def test_prompt_failures_clear_typed_and_legacy_state() -> None:
    from guardbot.bot.handlers import commands, features

    class CommandMessage:
        from_user = SimpleNamespace(id=987)
        chat = SimpleNamespace(type="private")

        async def delete(self) -> None:
            return None

        async def answer(self, text, **kwargs) -> None:
            raise TelegramBadRequest(
                method=SendMessage(chat_id=987, text="prompt"),
                message="send failed",
            )

    class CallbackMessage:
        chat = SimpleNamespace(type="private")

        async def edit_text(self, text, **kwargs) -> None:
            raise TelegramBadRequest(
                method=EditMessageText(text="prompt", chat_id=987, message_id=1),
                message="edit failed",
            )

    class Callback:
        from_user = SimpleNamespace(id=987)
        message = CallbackMessage()

        async def answer(self, *args, **kwargs) -> None:
            return None

    async def scenario() -> None:
        state = _state()
        with pytest.raises(TelegramBadRequest):
            await commands.cmd_unlock(
                CommandMessage(),
                SimpleNamespace(args=None),
                state,
            )
        assert await state.get_state() is None

        with pytest.raises(TelegramBadRequest):
            await commands.cmd_set_password(
                CommandMessage(),
                SimpleNamespace(args=None),
                state,
            )
        assert 987 not in commands.user_states

        with pytest.raises(TelegramBadRequest):
            await features.callback_set_password(Callback(), state)
        assert 987 not in features.user_states

    asyncio.run(scenario())


def test_password_cancel_clears_both_state_stores(monkeypatch) -> None:
    from guardbot.bot.handlers import accounts, features
    from guardbot.bot.states import AccountAddFlow

    called = False

    async def fake_settings(message, user_id) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(accounts, "update_settings_message", fake_settings)

    class Callback:
        from_user = SimpleNamespace(id=987)
        message = object()

        async def answer(self) -> None:
            return None

    callback = Callback()

    async def scenario() -> None:
        state = _state()
        await state.set_state(AccountAddFlow.waiting_credentials)
        features.user_states[987] = {
            "state": "waiting_bot_password",
            "action": "set",
        }

        await features.callback_cancel_password_to_settings(callback, state)

        assert called is True
        assert 987 not in features.user_states
        assert await state.get_state() is None

    asyncio.run(scenario())


def test_secret_message_is_deleted_before_automatic_lock(monkeypatch) -> None:
    from guardbot.bot import runtime

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
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            return Result()

        async def commit(self) -> None:
            return None

    class Message:
        from_user = SimpleNamespace(id=987)
        chat = SimpleNamespace(type="private")
        text = "login:password"

        def __init__(self) -> None:
            self.deleted = False

        async def delete(self) -> None:
            self.deleted = True

        async def answer(self, text, **kwargs) -> None:
            return None

    monkeypatch.setattr(runtime, "AsyncSessionLocal", lambda: Session())

    async def scenario() -> None:
        runtime.user_states[987] = {"state": "waiting_steam_code"}
        message = Message()
        event = SimpleNamespace(
            message=message,
            callback_query=None,
            inline_query=None,
        )
        try:
            async def forbidden_handler(event, data):
                raise AssertionError("timed-out secret must not be dispatched")

            await runtime.LockCheckMiddleware()(
                forbidden_handler,
                event,
                {"raw_state": None, "state": _state()},
            )
            assert message.deleted is True
            assert 987 not in runtime.user_states
        finally:
            runtime.user_states.pop(987, None)

    asyncio.run(scenario())


@pytest.mark.parametrize("file_name", ["accounts.txt", "guard.mafile", "data.json", "dump.zip"])
def test_non_private_secret_document_is_deleted_before_database(
    monkeypatch,
    file_name,
) -> None:
    from guardbot.bot import runtime

    class Message:
        from_user = SimpleNamespace(id=987)
        chat = SimpleNamespace(type="group")
        text = None

        def __init__(self) -> None:
            self.document = SimpleNamespace(file_name=file_name)
            self.deleted = False

        async def delete(self) -> None:
            self.deleted = True

        async def answer(self, text, **kwargs) -> None:
            return None

    monkeypatch.setattr(
        runtime,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not be opened")),
    )

    async def scenario() -> None:
        message = Message()
        event = SimpleNamespace(
            message=message,
            callback_query=None,
            inline_query=None,
        )

        async def forbidden_handler(event, data):
            raise AssertionError("secret document must not be dispatched")

        await runtime.LockCheckMiddleware()(
            forbidden_handler,
            event,
            {"raw_state": None, "state": _state()},
        )
        assert message.deleted is True

    asyncio.run(scenario())


def test_navigation_callback_clears_legacy_secret_state(monkeypatch) -> None:
    from guardbot.bot import runtime

    user = SimpleNamespace(bot_password=None, last_activity=None, lock_timeout=None)

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

    class Callback:
        from_user = SimpleNamespace(id=987)
        data = "account_detail_123"
        message = SimpleNamespace(chat=SimpleNamespace(type="private"))

    monkeypatch.setattr(runtime, "AsyncSessionLocal", lambda: Session())

    async def scenario() -> None:
        runtime.user_states[987] = {
            "state": "waiting_steam_code",
            "client": object(),
        }
        event = SimpleNamespace(
            message=None,
            callback_query=Callback(),
            inline_query=None,
        )

        async def handler(event, data):
            assert 987 not in runtime.user_states
            return "handled"

        try:
            result = await runtime.LockCheckMiddleware()(
                handler,
                event,
                {"raw_state": None, "state": _state()},
            )
            assert result == "handled"
        finally:
            runtime.user_states.pop(987, None)

    asyncio.run(scenario())


def test_prompt_error_middleware_clears_secret_state() -> None:
    from guardbot.bot import runtime

    event = SimpleNamespace(
        message=SimpleNamespace(from_user=SimpleNamespace(id=987)),
        callback_query=None,
        inline_query=None,
    )

    async def scenario() -> None:
        async def handler(event, data):
            await runtime.set_legacy_user_state(
                987,
                {"state": "waiting_steam_code"},
            )
            raise TelegramBadRequest(
                method=SendMessage(chat_id=987, text="prompt"),
                message="send failed",
            )

        try:
            with pytest.raises(TelegramBadRequest):
                await runtime.SecretStateCleanupMiddleware()(
                    handler,
                    event,
                    {"state": _state()},
                )
            assert 987 not in runtime.user_states
        finally:
            runtime.user_states.pop(987, None)

    asyncio.run(scenario())


def test_secret_delete_failure_aborts_before_database_and_dispatch(monkeypatch) -> None:
    from guardbot.bot import runtime

    class Message:
        from_user = SimpleNamespace(id=987)
        chat = SimpleNamespace(type="private")
        text = "login:password"

        def __init__(self) -> None:
            self.answers = []

        async def delete(self) -> None:
            raise TelegramBadRequest(
                method=DeleteMessage(chat_id=987, message_id=1),
                message="delete failed",
            )

        async def answer(self, text, **kwargs) -> None:
            self.answers.append(text)

    monkeypatch.setattr(
        runtime,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not be opened")),
    )

    async def scenario() -> None:
        runtime.user_states[987] = {"state": "waiting_steam_code"}
        message = Message()
        event = SimpleNamespace(
            message=message,
            callback_query=None,
            inline_query=None,
        )
        called = False

        async def handler(event, data):
            nonlocal called
            called = True

        try:
            await runtime.LockCheckMiddleware()(
                handler,
                event,
                {"raw_state": None, "state": _state()},
            )
            assert called is False
            assert "не удалось удалить" in message.answers[0].lower()
        finally:
            runtime.user_states.pop(987, None)

    asyncio.run(scenario())


def test_cancel_closes_legacy_state_resources() -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import BotPasswordFlow

    class Resource:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    class Message:
        from_user = SimpleNamespace(id=987)

        async def answer(self, text, **kwargs) -> None:
            return None

    async def scenario() -> None:
        state = _state()
        await state.set_state(BotPasswordFlow.waiting_password)
        await state.set_data({"action": "set"})
        client = Resource()
        db_session = Resource()
        accounts.user_states[987] = {
            "state": "waiting_steam_code",
            "client": client,
            "db_session": db_session,
        }
        await accounts.cmd_cancel(Message(), state)
        assert await state.get_state() is None
        assert await state.get_data() == {}
        assert 987 not in accounts.user_states
        assert client.closed is True
        assert db_session.closed is True

    asyncio.run(scenario())


def test_legacy_secret_state_timeout_closes_live_client() -> None:
    from guardbot.bot import runtime

    class Client:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    async def scenario() -> None:
        client = Client()
        runtime.user_states[987] = {
            "state": "waiting_steam_code_or_mafile",
            "client": client,
        }
        await runtime.set_legacy_user_state(
            987,
            {"state": "waiting_email_code", "client": client},
            ttl_seconds=0.01,
        )
        assert client.closed is False
        await asyncio.sleep(0.03)
        assert 987 not in runtime.user_states
        assert client.closed is True

    asyncio.run(scenario())


def test_password_prompt_callback_is_acknowledged_before_edit_failure() -> None:
    from guardbot.bot.handlers import features

    class Message:
        chat = SimpleNamespace(type="private")

        async def edit_text(self, text, **kwargs) -> None:
            raise TelegramBadRequest(
                method=EditMessageText(text="prompt", chat_id=987, message_id=1),
                message="edit failed",
            )

    class Callback:
        from_user = SimpleNamespace(id=987)
        message = Message()

        def __init__(self) -> None:
            self.acks = 0

        async def answer(self, *args, **kwargs) -> None:
            self.acks += 1

    async def scenario() -> None:
        callback = Callback()
        with pytest.raises(TelegramBadRequest):
            await features.callback_set_password(callback, _state())
        assert callback.acks == 1

    asyncio.run(scenario())


def test_cancel_command_clears_both_stores_before_lock_database_failure(
    monkeypatch,
) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.states import BotPasswordFlow

    class Filter:
        def __init__(self, result) -> None:
            self.result = result

        async def __call__(self, message, bot):
            return self.result

    class Session:
        async def __aenter__(self):
            raise RuntimeError("database unavailable")

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Message:
        from_user = SimpleNamespace(id=993)
        chat = SimpleNamespace(type="private")
        text = "/cancel"
        document = None

        async def delete(self) -> None:
            return None

        async def answer(self, *args, **kwargs) -> None:
            return None

    monkeypatch.setattr(runtime, "_CANCEL_COMMAND_FILTER", Filter(object()))
    for name in (
        "_UNLOCK_COMMAND_FILTER",
        "_SET_PASSWORD_COMMAND_FILTER",
        "_SET_PROXY_COMMAND_FILTER",
        "_SET_ACCOUNT_PROXY_COMMAND_FILTER",
        "_IMPORT_MAFILE_COMMAND_FILTER",
        "_BULK_IMPORT_COMMAND_FILTER",
        "_LOGIN_COMMAND_FILTER",
        "_NOTE_COMMAND_FILTER",
    ):
        monkeypatch.setattr(runtime, name, Filter(None))
    monkeypatch.setattr(runtime, "AsyncSessionLocal", Session)

    async def scenario() -> None:
        state = _state(993)
        await state.set_state(BotPasswordFlow.waiting_password)
        await state.set_data({"action": "set"})
        runtime.user_states[993] = {"state": "waiting_steam_code"}

        async def handler(event, data):
            raise AssertionError("lock database failure must precede dispatch")

        try:
            event = SimpleNamespace(
                message=Message(),
                callback_query=None,
                inline_query=None,
            )
            with pytest.raises(RuntimeError, match="database unavailable"):
                await runtime.LockCheckMiddleware()(
                    handler,
                    event,
                    {
                        "bot": object(),
                        "raw_state": BotPasswordFlow.waiting_password.state,
                        "state": state,
                    },
                )

            assert 993 not in runtime.user_states
            assert await state.get_state() is None
            assert await state.get_data() == {}
        finally:
            await runtime.clear_legacy_user_state(993)
            await state.clear()

    asyncio.run(scenario())


def test_cancel_command_mentions_only_affect_the_current_bot(monkeypatch) -> None:
    from aiogram.types import Chat, Message, User

    from guardbot.bot import runtime
    from guardbot.bot.states import BotPasswordFlow

    class FakeBot:
        async def me(self) -> User:
            return User(
                id=42,
                is_bot=True,
                first_name="Guard",
                username="TestBot",
            )

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return None

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            return Result()

    class Resource:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(runtime, "AsyncSessionLocal", Session)

    async def run_case(text: str, *, should_cancel: bool) -> None:
        state = _state(995)
        await state.set_state(BotPasswordFlow.waiting_password)
        await state.set_data({"action": "set"})
        resource = Resource()
        runtime.user_states[995] = {
            "state": "waiting_steam_code",
            "client": resource,
        }
        message = Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=995, type="private"),
            from_user=User(
                id=995,
                is_bot=False,
                first_name="Tester",
            ),
            text=text,
        )
        dispatched = False

        async def handler(_event, _data):
            nonlocal dispatched
            dispatched = True

        try:
            await runtime.LockCheckMiddleware()(
                handler,
                SimpleNamespace(
                    message=message,
                    callback_query=None,
                    inline_query=None,
                ),
                {
                    "bot": FakeBot(),
                    "raw_state": BotPasswordFlow.waiting_password.state,
                    "state": state,
                },
            )

            if should_cancel:
                assert 995 not in runtime.user_states
                assert await state.get_state() is None
                assert await state.get_data() == {}
                assert resource.closed is True
                assert dispatched is True
            else:
                assert runtime.user_states[995]["state"] == "waiting_steam_code"
                assert await state.get_state() == BotPasswordFlow.waiting_password.state
                assert await state.get_data() == {"action": "set"}
                assert resource.closed is False
                assert dispatched is False
        finally:
            await runtime.clear_user_workflows(995, state)

    async def scenario() -> None:
        await run_case("/cancel", should_cancel=True)
        await run_case("/cancel@TestBot", should_cancel=True)
        await run_case("/cancel@OtherBot", should_cancel=False)

    asyncio.run(scenario())


def test_caption_commands_and_untexted_secret_documents_are_safe(monkeypatch) -> None:
    from aiogram.types import Chat, Document, Message, User

    from guardbot.bot import runtime
    from guardbot.bot.states import BotPasswordFlow

    class FakeBot:
        async def me(self) -> User:
            return User(
                id=42,
                is_bot=True,
                first_name="Guard",
                username="TestBot",
            )

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return None

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            return Result()

    class Resource:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    deleted_messages = []

    async def record_secret_deletion(message) -> bool:
        deleted_messages.append(message)
        return True

    monkeypatch.setattr(runtime, "AsyncSessionLocal", Session)
    monkeypatch.setattr(
        runtime,
        "delete_secret_message_or_abort",
        record_secret_deletion,
    )

    def make_document_message(caption: str | None) -> Message:
        return Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=996, type="private"),
            from_user=User(
                id=996,
                is_bot=False,
                first_name="Tester",
            ),
            document=Document(
                file_id="secret-file",
                file_unique_id="secret-unique",
                file_name="guard.mafile",
            ),
            caption=caption,
        )

    async def run_caption_case(
        caption: str,
        *,
        should_cancel: bool,
        should_delete: bool,
    ) -> None:
        state = _state(996)
        await state.set_state(BotPasswordFlow.waiting_password)
        await state.set_data({"action": "set"})
        resource = Resource()
        runtime.user_states[996] = {
            "state": "waiting_steam_code",
            "client": resource,
        }
        message = make_document_message(caption)
        dispatched = False
        deletions_before = len(deleted_messages)

        async def handler(_event, _data):
            nonlocal dispatched
            dispatched = True

        try:
            await runtime.LockCheckMiddleware()(
                handler,
                SimpleNamespace(
                    message=message,
                    callback_query=None,
                    inline_query=None,
                ),
                {
                    "bot": FakeBot(),
                    "raw_state": BotPasswordFlow.waiting_password.state,
                    "state": state,
                },
            )

            assert len(deleted_messages) - deletions_before == int(should_delete)
            if should_cancel:
                assert 996 not in runtime.user_states
                assert await state.get_state() is None
                assert await state.get_data() == {}
                assert resource.closed is True
                assert dispatched is True
            else:
                assert runtime.user_states[996]["state"] == "waiting_steam_code"
                assert await state.get_state() == BotPasswordFlow.waiting_password.state
                assert await state.get_data() == {"action": "set"}
                assert resource.closed is False
                assert dispatched is False
        finally:
            await runtime.clear_user_workflows(996, state)

    async def scenario() -> None:
        plain_document = make_document_message(None)
        dispatched = False

        async def plain_handler(_event, _data):
            nonlocal dispatched
            dispatched = True

        await runtime.LockCheckMiddleware()(
            plain_handler,
            SimpleNamespace(
                message=plain_document,
                callback_query=None,
                inline_query=None,
            ),
            {"bot": FakeBot(), "raw_state": None, "state": _state(996)},
        )
        assert deleted_messages == [plain_document]
        assert dispatched is True

        await run_caption_case(
            "/cancel@TestBot",
            should_cancel=True,
            should_delete=True,
        )
        await run_caption_case(
            "/cancel@testbot",
            should_cancel=True,
            should_delete=True,
        )
        await run_caption_case(
            "/cancel@OtherBot",
            should_cancel=False,
            should_delete=False,
        )
        await run_caption_case(
            "/unknown@OtherBot",
            should_cancel=False,
            should_delete=False,
        )

    asyncio.run(scenario())


def test_navigation_clears_typed_state_after_clearing_parallel_legacy_state(
    monkeypatch,
) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.states import AccountAddFlow

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

    class Callback:
        from_user = SimpleNamespace(id=994)
        data = "settings"
        message = SimpleNamespace(chat=SimpleNamespace(type="private"))

    monkeypatch.setattr(runtime, "AsyncSessionLocal", Session)

    async def scenario() -> None:
        state = _state(994)
        await state.set_state(AccountAddFlow.waiting_credentials)
        runtime.user_states[994] = {"state": "waiting_steam_code"}
        handled = False

        async def handler(event, data):
            nonlocal handled
            handled = True
            assert 994 not in runtime.user_states

        event = SimpleNamespace(
            message=None,
            callback_query=Callback(),
            inline_query=None,
        )
        await runtime.LockCheckMiddleware()(
            handler,
            event,
            {
                "raw_state": AccountAddFlow.waiting_credentials.state,
                "state": state,
            },
        )

        assert handled is True
        assert await state.get_state() is None

    asyncio.run(scenario())


def test_callback_acknowledgement_middleware_answers_exactly_once_before_handler() -> None:
    from guardbot.bot import runtime

    middleware_types = [
        type(middleware)
        for middleware in runtime.dp.update.outer_middleware._middlewares
    ]
    assert middleware_types.index(runtime.CallbackAcknowledgeMiddleware) < (
        middleware_types.index(type(runtime.dp.fsm))
    )
    assert middleware_types.index(type(runtime.dp.fsm)) < (
        middleware_types.index(runtime.LockCheckMiddleware)
    )
    assert middleware_types.index(runtime.CallbackAcknowledgeMiddleware) < (
        middleware_types.index(runtime.LockCheckMiddleware)
    )
    assert middleware_types.index(runtime.LockCheckMiddleware) < (
        middleware_types.index(runtime.SecretStateCleanupMiddleware)
    )

    class Message:
        chat = SimpleNamespace(type="private")

        def __init__(self) -> None:
            self.feedback: list[str] = []

        async def answer(self, text: str, **kwargs) -> None:
            self.feedback.append(text)

    class Callback:
        def __init__(self) -> None:
            self.answer_calls = 0
            self.message = Message()

        async def answer(self, *args, **kwargs) -> None:
            self.answer_calls += 1

    async def scenario() -> None:
        callback = Callback()
        event = SimpleNamespace(callback_query=callback)

        async def handler(_event, _data):
            assert callback.answer_calls == 1
            await runtime.callback_feedback(callback, "Обычное уведомление")
            await runtime.callback_feedback(
                callback,
                "Негромкое уведомление",
                show_alert=False,
            )
            await runtime.callback_feedback(
                callback,
                "Ошибка операции",
                show_alert=True,
            )
            await runtime.callback_feedback(callback)

        await runtime.CallbackAcknowledgeMiddleware()(handler, event, {})
        assert callback.answer_calls == 1
        assert callback.message.feedback == [
            "Обычное уведомление",
            "Негромкое уведомление",
            "Ошибка операции",
        ]

    asyncio.run(scenario())


def test_composed_dispatcher_acknowledges_callback_before_fsm_lock() -> None:
    from aiogram import Bot
    from aiogram.methods import AnswerCallbackQuery
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    from guardbot.bot import runtime

    class FakeBot(Bot):
        def __init__(self) -> None:
            super().__init__(token="42:TEST")
            self.calls = []

        async def __call__(self, method, request_timeout=None):
            self.calls.append(method)
            if isinstance(method, AnswerCallbackQuery):
                return True
            raise AssertionError(f"Unexpected Bot API method: {method!r}")

    async def scenario() -> None:
        bot = FakeBot()
        user = User(id=98_765, is_bot=False, first_name="Test")
        update = Update(
            update_id=1,
            callback_query=CallbackQuery(
                id="callback-1",
                from_user=user,
                chat_instance="private-chat",
                data="page_info",
                message=Message(
                    message_id=1,
                    date=datetime.now(UTC),
                    chat=Chat(id=user.id, type="private"),
                    from_user=user,
                    text="menu",
                ),
            ),
        )
        key = StorageKey(bot_id=bot.id, chat_id=user.id, user_id=user.id)

        async with runtime.dp.fsm.events_isolation.lock(key):
            task = asyncio.create_task(runtime.dp.feed_update(bot, update))
            for _ in range(20):
                if any(isinstance(call, AnswerCallbackQuery) for call in bot.calls):
                    break
                await asyncio.sleep(0)
            assert any(isinstance(call, AnswerCallbackQuery) for call in bot.calls)
            assert task.done() is False
            task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_composed_dispatcher_rejects_group_callback_before_fsm_lock() -> None:
    from aiogram import Bot
    from aiogram.methods import AnswerCallbackQuery, SendMessage
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    from guardbot.bot import runtime

    class FakeBot(Bot):
        def __init__(self) -> None:
            super().__init__(token="42:TEST")
            self.calls = []

        async def __call__(self, method, request_timeout=None):
            self.calls.append(method)
            if isinstance(method, AnswerCallbackQuery):
                return True
            if isinstance(method, SendMessage):
                return Message(
                    message_id=2,
                    date=datetime.now(UTC),
                    chat=Chat(id=-100, type="group"),
                    text=method.text,
                )
            raise AssertionError(f"Unexpected Bot API method: {method!r}")

    async def scenario() -> None:
        bot = FakeBot()
        user = User(id=98_766, is_bot=False, first_name="Test")
        update = Update(
            update_id=2,
            callback_query=CallbackQuery(
                id="callback-2",
                from_user=user,
                chat_instance="group-chat",
                data="page_info",
                message=Message(
                    message_id=1,
                    date=datetime.now(UTC),
                    chat=Chat(id=-100, type="group"),
                    from_user=user,
                    text="stale menu",
                ),
            ),
        )
        key = StorageKey(bot_id=bot.id, chat_id=user.id, user_id=user.id)

        async with runtime.dp.fsm.events_isolation.lock(key):
            await asyncio.wait_for(runtime.dp.feed_update(bot, update), timeout=1)

        assert sum(isinstance(call, AnswerCallbackQuery) for call in bot.calls) == 1
        feedback = [call.text for call in bot.calls if isinstance(call, SendMessage)]
        assert feedback and "личном чате" in feedback[0]

    asyncio.run(scenario())


@pytest.mark.parametrize("blocked_operation", ["get_state", "clear"])
def test_typed_secret_cleanup_resists_repeated_cancellation(
    blocked_operation: str,
) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.states import UnlockFlow

    class BlockingFSMContext(FSMContext):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.blocked = asyncio.Event()
            self.release = asyncio.Event()

        async def get_state(self):
            if blocked_operation == "get_state":
                self.blocked.set()
                await self.release.wait()
            return await super().get_state()

        async def clear(self) -> None:
            if blocked_operation == "clear":
                self.blocked.set()
                await self.release.wait()
            await super().clear()

    async def scenario() -> None:
        storage = MemoryStorage()
        key = StorageKey(bot_id=42, chat_id=995, user_id=995)
        state = BlockingFSMContext(storage=storage, key=key)
        await state.set_state(UnlockFlow.waiting_password)
        entered = asyncio.Event()
        event = SimpleNamespace(
            message=SimpleNamespace(from_user=SimpleNamespace(id=995)),
            callback_query=None,
            inline_query=None,
        )

        async def handler(_event, _data):
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(
            runtime.SecretStateCleanupMiddleware()(
                handler,
                event,
                {"state": state},
            )
        )
        await entered.wait()
        task.cancel()
        await state.blocked.wait()
        task.cancel()
        state.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await storage.get_state(key) is None

    asyncio.run(scenario())
