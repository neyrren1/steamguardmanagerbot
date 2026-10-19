import asyncio
import inspect
from datetime import UTC, datetime
from types import SimpleNamespace

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.strategy import FSMStrategy
from aiogram.methods import DeleteMessage, SendMessage
from aiogram.types import Chat, Message, MessageEntity, Update, User


def _state(user_id: int = 987) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=42, chat_id=user_id, user_id=user_id),
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


def test_dispatcher_serializes_each_users_fsm_updates() -> None:
    from guardbot.bot import runtime

    assert isinstance(runtime.dp.storage, MemoryStorage)
    assert isinstance(runtime.dp.fsm.events_isolation, runtime.EvictingEventIsolation)
    assert runtime.dp.fsm.strategy is FSMStrategy.GLOBAL_USER


def test_legacy_account_state_name_is_removed() -> None:
    from guardbot.bot.handlers import accounts, messages

    for module in (accounts, messages):
        assert "waiting_steam_credentials" not in inspect.getsource(module)


def test_invalid_account_credentials_keep_fsm_active_for_retry() -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import AccountAddFlow

    class Message:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=987)
            self.chat = SimpleNamespace(type="private")
            self.text = "not-a-login-password-pair"
            self.answers = []
            self.deleted = False

        async def delete(self) -> None:
            self.deleted = True

        async def answer(self, text, **kwargs) -> None:
            self.answers.append((text, kwargs))

    async def scenario() -> None:
        state = _state()
        await state.set_state(AccountAddFlow.waiting_credentials)
        message = Message()

        await accounts.receive_account_credentials(message, state)

        assert message.deleted is True
        assert message.answers == [
            ("❌ Неверный формат. Используйте: account_name:password", {})
        ]
        assert await state.get_state() == AccountAddFlow.waiting_credentials.state
        assert await state.get_data() == {}

    asyncio.run(scenario())


def test_incomplete_account_credentials_keep_fsm_active_for_retry() -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import AccountAddFlow

    class Message:
        def __init__(self, text: str) -> None:
            self.from_user = SimpleNamespace(id=987)
            self.chat = SimpleNamespace(type="private")
            self.text = text
            self.answers = []

        async def delete(self) -> None:
            return None

        async def answer(self, text, **kwargs) -> None:
            self.answers.append((text, kwargs))

    async def scenario() -> None:
        for credentials in (":password", "login:"):
            state = _state()
            await state.set_state(AccountAddFlow.waiting_credentials)
            message = Message(credentials)

            await accounts.receive_account_credentials(message, state)

            assert message.answers == [
                ("❌ Неверный формат. Используйте: account_name:password", {})
            ]
            assert await state.get_state() == AccountAddFlow.waiting_credentials.state

    asyncio.run(scenario())


def test_valid_account_credentials_are_encrypted_and_clear_fsm(monkeypatch) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import AccountAddFlow

    class Result:
        def __init__(self, value) -> None:
            self.value = value

        def scalar_one_or_none(self):
            return self.value

        def scalar(self):
            return self.value

    class Session:
        def __init__(self) -> None:
            self.execute_calls = 0
            self.added = None
            self.committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            self.execute_calls += 1
            return Result(None if self.execute_calls == 1 else 4)

        def add(self, mafile) -> None:
            self.added = mafile
            mafile.id = 123

        async def commit(self) -> None:
            self.committed = True

    class Message:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=987)
            self.chat = SimpleNamespace(type="private")
            self.text = "  test-login:secret-password  "
            self.answers = []
            self.deleted = False

        async def delete(self) -> None:
            self.deleted = True

        async def answer(self, text, **kwargs) -> None:
            self.answers.append((text, kwargs))

    session = Session()
    monkeypatch.setattr(accounts, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(accounts, "encrypt_value", lambda value: f"enc:{value}")

    async def scenario() -> None:
        state = _state()
        await state.set_state(AccountAddFlow.waiting_credentials)
        message = Message()

        await accounts.receive_account_credentials(message, state)

        assert message.deleted is True
        assert session.committed is True
        assert session.added is not None
        assert session.added.telegram_id == 987
        assert session.added.account_name == "test-login"
        assert session.added.password == "enc:secret-password"
        assert session.added.sort_order == 5
        assert await state.get_state() is None
        assert await state.get_data() == {}
        assert "Аккаунт test-login добавлен" in message.answers[0][0]
        callback_data = [
            button.callback_data
            for row in message.answers[0][1]["reply_markup"].inline_keyboard
            for button in row
        ]
        assert "action_login_123" in callback_data
        assert "login_account_123" not in callback_data

    asyncio.run(scenario())


def test_post_commit_notification_failure_is_not_reported_as_add_failure(
    monkeypatch,
) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import AccountAddFlow

    class Result:
        def __init__(self, value) -> None:
            self.value = value

        def scalar_one_or_none(self):
            return self.value

        def scalar(self):
            return self.value

    class Session:
        def __init__(self) -> None:
            self.execute_calls = 0
            self.committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            self.execute_calls += 1
            return Result(None if self.execute_calls == 1 else 0)

        def add(self, mafile) -> None:
            mafile.id = 123

        async def commit(self) -> None:
            self.committed = True

    class Message:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=987)
            self.chat = SimpleNamespace(type="private")
            self.text = "login:password"
            self.answer_calls = 0

        async def delete(self) -> None:
            return None

        async def answer(self, text, **kwargs) -> None:
            self.answer_calls += 1
            raise TelegramBadRequest(
                method=SendMessage(chat_id=987, text=text),
                message="send failed",
            )

    session = Session()
    monkeypatch.setattr(accounts, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(accounts, "encrypt_value", lambda value: f"enc:{value}")

    async def scenario() -> None:
        state = _state()
        await state.set_state(AccountAddFlow.waiting_credentials)
        message = Message()

        try:
            await accounts.receive_account_credentials(message, state)
        except TelegramBadRequest:
            pass
        else:
            raise AssertionError("TelegramBadRequest was not propagated")

        assert session.committed is True
        assert message.answer_calls == 1
        assert await state.get_state() is None

    asyncio.run(scenario())


def test_add_command_starts_local_flow_without_proxy(monkeypatch) -> None:
    from guardbot.bot import pending_challenges, runtime
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import AccountAddFlow

    state = _state()
    client = _ChallengeClient()

    class Message:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=987)
            self.chat = SimpleNamespace(type="private")
            self.answers = []
            self.states_during_answer = []

        async def answer(self, text, **kwargs) -> None:
            self.states_during_answer.append(await state.get_state())
            self.answers.append((text, kwargs))

    async def no_proxy(*args, **kwargs):
        return False, None

    monkeypatch.setattr(runtime, "check_proxy_available", no_proxy)

    async def scenario() -> None:
        message = Message()
        await _publish_challenge(987, state, client)

        try:
            await accounts.cmd_add_steam(message, state)

            assert await state.get_state() == AccountAddFlow.waiting_credentials.state
            assert await state.get_data() == {}
            assert client.close_calls == 1
            assert pending_challenges.current_pending_challenge_token(987) is None
            assert message.states_during_answer == [
                AccountAddFlow.waiting_credentials.state
            ]
            assert "account_name:password" in message.answers[0][0]
            assert "Нет доступного прокси" not in message.answers[0][0]
        finally:
            await pending_challenges.clear_pending_challenges_for_user(987, state)

    asyncio.run(scenario())


def test_cancel_command_clears_account_add_fsm() -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import AccountAddFlow

    class Message:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=987)
            self.chat = SimpleNamespace(type="private")
            self.answers = []

        async def answer(self, text, **kwargs) -> None:
            self.answers.append((text, kwargs))

    async def scenario() -> None:
        state = _state()
        await state.set_state(AccountAddFlow.waiting_credentials)
        message = Message()

        await accounts.cmd_cancel(message, state)

        assert await state.get_state() is None
        assert message.answers == [
            (
                "❌ Добавление аккаунта отменено.\nИспользуйте /accounts для просмотра списка.",
                {},
            )
        ]

    asyncio.run(scenario())


def test_add_command_clears_fsm_when_prompt_cannot_be_sent() -> None:
    from guardbot.bot.handlers import accounts

    class Message:
        from_user = SimpleNamespace(id=987)
        chat = SimpleNamespace(type="private")

        async def answer(self, text, **kwargs) -> None:
            raise TelegramBadRequest(
                method=SendMessage(chat_id=987, text="prompt"),
                message="send failed",
            )

    async def scenario() -> None:
        state = _state()

        try:
            await accounts.cmd_add_steam(Message(), state)
        except TelegramBadRequest:
            pass
        else:
            raise AssertionError("TelegramBadRequest was not propagated")

        assert await state.get_state() is None

    asyncio.run(scenario())


def test_add_command_clears_fsm_on_non_telegram_prompt_failure() -> None:
    from guardbot.bot.handlers import accounts

    class Message:
        from_user = SimpleNamespace(id=987)
        chat = SimpleNamespace(type="private")

        async def answer(self, text, **kwargs) -> None:
            raise RuntimeError("transport failed")

    async def scenario() -> None:
        state = _state()

        try:
            await accounts.cmd_add_steam(Message(), state)
        except RuntimeError:
            pass
        else:
            raise AssertionError("RuntimeError was not propagated")

        assert await state.get_state() is None

    asyncio.run(scenario())


def test_dispatcher_routes_add_command_and_credentials_through_fsm(
    monkeypatch,
) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import accounts, messages  # noqa: F401
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
                    message_id=100 + len(self.calls),
                    date=datetime.now(UTC),
                    chat=Chat(id=987, type="private"),
                    from_user=User(
                        id=self.id,
                        is_bot=True,
                        first_name="GuardBot",
                    ),
                    text=method.text,
                )
            raise AssertionError(f"Unexpected Bot API method: {method!r}")

    def update(update_id: int, text: str, *, command: bool = False) -> Update:
        entities = None
        if command:
            entities = [
                MessageEntity(type="bot_command", offset=0, length=len(text))
            ]
        return Update(
            update_id=update_id,
            message=Message(
                message_id=update_id,
                date=datetime.now(UTC),
                chat=Chat(id=987, type="private"),
                from_user=User(id=987, is_bot=False, first_name="Test"),
                text=text,
                entities=entities,
            ),
        )

    monkeypatch.setattr(runtime, "AsyncSessionLocal", lambda: Session())

    async def scenario() -> None:
        bot = FakeBot()
        state = runtime.dp.fsm.get_context(bot=bot, chat_id=987, user_id=987)
        await state.clear()
        try:
            await runtime.dp.feed_update(bot, update(1, "/add", command=True))
            assert await state.get_state() == AccountAddFlow.waiting_credentials.state

            await runtime.dp.feed_update(bot, update(2, "invalid"))
            assert await state.get_state() == AccountAddFlow.waiting_credentials.state
            assert await state.get_data() == {}

            sent_texts = [
                method.text for method in bot.calls if isinstance(method, SendMessage)
            ]
            assert any("account_name:password" in text for text in sent_texts)
            assert sent_texts[-1] == (
                "❌ Неверный формат. Используйте: account_name:password"
            )
            assert not any(
                "Используйте команды для управления" in text
                for text in sent_texts
            )
        finally:
            await state.clear()
            await bot.session.close()

    asyncio.run(scenario())
