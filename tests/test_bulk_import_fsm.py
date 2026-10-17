import asyncio
from types import SimpleNamespace


class FakeState:
    def __init__(self) -> None:
        self.state = None
        self.data = {"stale": True}
        self.clear_calls = 0

    async def clear(self) -> None:
        self.clear_calls += 1
        self.state = None
        self.data = {}

    async def set_state(self, state) -> None:
        self.state = state.state

    async def set_data(self, data: dict) -> None:
        self.data = dict(data)


class PromptMessage:
    def __init__(self, state: FakeState, user_id: int) -> None:
        self.state = state
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=user_id, type="private")
        self.snapshots: list[tuple[str | None, dict]] = []

    async def answer(self, *_args, **_kwargs) -> None:
        self.snapshots.append((self.state.state, dict(self.state.data)))

    async def edit_text(self, *_args, **_kwargs) -> None:
        self.snapshots.append((self.state.state, dict(self.state.data)))


def test_bulk_import_flow_has_typed_credentials_state() -> None:
    from guardbot.bot import states

    flow = getattr(states, "BulkImportFlow", None)

    assert flow is not None
    assert flow.waiting_credentials.state == "BulkImportFlow:waiting_credentials"


def test_bulk_import_command_starts_typed_flow_without_proxy(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import common
    from guardbot.bot.states import BulkImportFlow

    user_id = 801
    state = FakeState()
    message = PromptMessage(state, user_id)
    runtime.user_states[user_id] = {"state": "waiting_mafile"}

    async def proxy_lookup_must_not_run(*_args, **_kwargs):
        raise AssertionError("local credential collection must not require a proxy")

    monkeypatch.setattr(runtime, "check_proxy_available", proxy_lookup_must_not_run)

    async def scenario() -> None:
        try:
            await common.cmd_bulk_import(message, state=state)

            assert user_id not in runtime.user_states
            assert state.state == BulkImportFlow.waiting_credentials.state
            assert state.data == {}
            assert message.snapshots == [
                (BulkImportFlow.waiting_credentials.state, {})
            ]
        finally:
            await runtime.clear_legacy_user_state(user_id)

    asyncio.run(scenario())


def test_bulk_import_callback_starts_typed_flow_before_prompt(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import common
    from guardbot.bot.states import BulkImportFlow

    user_id = 802
    state = FakeState()
    message = PromptMessage(state, user_id)
    callback = SimpleNamespace(from_user=SimpleNamespace(id=user_id), message=message)
    runtime.user_states[user_id] = {"state": "waiting_mafile"}

    async def feedback(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(common, "callback_feedback", feedback)

    async def scenario() -> None:
        try:
            await common.callback_bulk_import_menu(callback, state)

            assert user_id not in runtime.user_states
            assert state.state == BulkImportFlow.waiting_credentials.state
            assert state.data == {}
            assert message.snapshots == [
                (BulkImportFlow.waiting_credentials.state, {})
            ]
        finally:
            await runtime.clear_legacy_user_state(user_id)

    asyncio.run(scenario())


def test_bulk_credentials_deletion_failure_aborts_before_database(monkeypatch) -> None:
    from guardbot.bot.handlers import bulk_import
    from guardbot.bot.states import BulkImportFlow

    state = FakeState()
    state.state = BulkImportFlow.waiting_credentials.state

    class Message:
        chat = SimpleNamespace(type="private")
        from_user = SimpleNamespace(id=803)
        text = "login:top-secret"

        def __init__(self) -> None:
            self.answers: list[str] = []

        async def delete(self) -> None:
            raise RuntimeError("Telegram delete failed")

        async def answer(self, text: str, **_kwargs) -> None:
            self.answers.append(text)

    def database_must_not_open():
        raise AssertionError("database must not open after secret deletion failure")

    monkeypatch.setattr(bulk_import, "AsyncSessionLocal", database_must_not_open)
    message = Message()

    asyncio.run(bulk_import.process_bulk_import(message, state))

    assert state.state == BulkImportFlow.waiting_credentials.state
    assert state.data == {"stale": True}
    assert message.answers == [
        "❌ Не удалось удалить сообщение с секретными данными. "
        "Данные не были обработаны; повторите попытку в личном чате."
    ]


def test_invalid_bulk_credentials_remain_retryable_without_database(monkeypatch) -> None:
    from guardbot.bot.handlers import bulk_import
    from guardbot.bot.states import BulkImportFlow

    state = FakeState()
    state.state = BulkImportFlow.waiting_credentials.state
    state.data = {}

    class Message:
        chat = SimpleNamespace(type="private")
        from_user = SimpleNamespace(id=804)
        text = "invalid-without-colon"

        def __init__(self) -> None:
            self.deleted = False
            self.answers: list[str] = []

        async def delete(self) -> None:
            self.deleted = True

        async def answer(self, text: str, **_kwargs) -> None:
            self.answers.append(text)

    def database_must_not_open():
        raise AssertionError("database must not open for invalid credentials")

    monkeypatch.setattr(bulk_import, "AsyncSessionLocal", database_must_not_open)
    message = Message()

    asyncio.run(bulk_import.process_bulk_import(message, state))

    assert message.deleted is True
    assert state.state == BulkImportFlow.waiting_credentials.state
    assert state.data == {}
    assert message.answers == [
        "❌ Ни один аккаунт не прошел проверку формата.\n"
        "Ожидается: <code>login:password</code>"
    ]


def test_successful_bulk_import_commits_then_clears_before_feedback(monkeypatch) -> None:
    from guardbot.bot.handlers import bulk_import
    from guardbot.bot.states import BulkImportFlow

    state = FakeState()
    state.state = BulkImportFlow.waiting_credentials.state
    state.data = {}

    class Result:
        @staticmethod
        def scalar():
            return 0

        @staticmethod
        def scalar_one_or_none():
            return None

    class Session:
        def __init__(self) -> None:
            self.added = []
            self.committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            return Result()

        def add(self, value) -> None:
            self.added.append(value)

        async def commit(self) -> None:
            self.committed = True

    class StatusMessage:
        def __init__(self) -> None:
            self.state_snapshots: list[tuple[str | None, dict]] = []

        async def edit_text(self, *_args, **_kwargs) -> None:
            self.state_snapshots.append((state.state, dict(state.data)))

    class Message:
        chat = SimpleNamespace(type="private")
        from_user = SimpleNamespace(id=805)
        text = "first:secret-password"

        def __init__(self) -> None:
            self.deleted = False
            self.status = StatusMessage()

        async def delete(self) -> None:
            self.deleted = True

        async def answer(self, *_args, **_kwargs):
            return self.status

    session = Session()
    monkeypatch.setattr(bulk_import, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(bulk_import, "encrypt_value", lambda value: f"enc:{value}")
    message = Message()

    asyncio.run(bulk_import.process_bulk_import(message, state))

    assert message.deleted is True
    assert session.committed is True
    assert len(session.added) == 1
    assert session.added[0].account_name == "first"
    assert session.added[0].password == "enc:secret-password"
    assert state.state is None
    assert state.data == {}
    assert message.status.state_snapshots == [(None, {})]


def test_removed_bulk_legacy_state_has_no_production_references() -> None:
    from pathlib import Path

    production_root = Path(__file__).resolve().parents[1] / "guardbot"
    references = [
        str(path.relative_to(production_root))
        for path in production_root.rglob("*.py")
        if "waiting_bulk_credentials" in path.read_text(encoding="utf-8")
    ]

    assert references == []


def test_dispatcher_routes_bulk_command_and_credentials_through_typed_fsm(
    monkeypatch,
) -> None:
    from datetime import UTC, datetime

    from aiogram import Bot
    from aiogram.methods import DeleteMessage, SendMessage
    from aiogram.types import Chat, Message, MessageEntity, Update, User

    from guardbot.bot import runtime
    from guardbot.bot.handlers import bulk_import, common, messages  # noqa: F401
    from guardbot.bot.states import BulkImportFlow

    user_id = 806

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return None

    class LockSession:
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
                    chat=Chat(id=method.chat_id, type="private"),
                    from_user=User(
                        id=self.id,
                        is_bot=True,
                        first_name="GuardBot",
                    ),
                    text=method.text,
                )
            raise AssertionError(f"Unexpected Bot API method: {method!r}")

    def bulk_database_must_not_open():
        raise AssertionError("invalid credentials must not open domain database")

    async def proxy_lookup_must_not_run(*_args, **_kwargs):
        raise AssertionError("bulk import collection must not require proxy")

    monkeypatch.setattr(runtime, "AsyncSessionLocal", LockSession)
    monkeypatch.setattr(bulk_import, "AsyncSessionLocal", bulk_database_must_not_open)
    monkeypatch.setattr(runtime, "check_proxy_available", proxy_lookup_must_not_run)

    def update(update_id: int, text: str, *, command: bool = False) -> Update:
        entities = (
            [MessageEntity(type="bot_command", offset=0, length=len(text))]
            if command
            else None
        )
        return Update(
            update_id=update_id,
            message=Message(
                message_id=update_id,
                date=datetime.now(UTC),
                chat=Chat(id=user_id, type="private"),
                from_user=User(id=user_id, is_bot=False, first_name="Test"),
                text=text,
                entities=entities,
            ),
        )

    async def scenario() -> None:
        bot = FakeBot()
        state = runtime.dp.fsm.get_context(
            bot=bot,
            chat_id=user_id,
            user_id=user_id,
        )
        await state.clear()
        try:
            await runtime.dp.feed_update(
                bot,
                update(1, "/import_bulk", command=True),
            )
            assert await state.get_state() == BulkImportFlow.waiting_credentials.state
            assert await state.get_data() == {}

            await runtime.dp.feed_update(bot, update(2, "invalid"))
            assert await state.get_state() == BulkImportFlow.waiting_credentials.state
            assert await state.get_data() == {}

            sent_texts = [
                method.text for method in bot.calls if isinstance(method, SendMessage)
            ]
            assert any("МАССОВЫЙ ИМПОРТ" in text for text in sent_texts)
            assert sent_texts[-1] == (
                "❌ Ни один аккаунт не прошел проверку формата.\n"
                "Ожидается: <code>login:password</code>"
            )
            assert not any(
                "Используйте команды для управления" in text
                for text in sent_texts
            )
        finally:
            await state.clear()
            await bot.session.close()

    asyncio.run(scenario())


def test_dispatcher_rejects_group_bulk_credentials_before_database(
    monkeypatch,
) -> None:
    from datetime import UTC, datetime

    from aiogram import Bot
    from aiogram.methods import DeleteMessage, SendMessage
    from aiogram.types import Chat, Message, Update, User

    from guardbot.bot import runtime
    from guardbot.bot.handlers import bulk_import, messages  # noqa: F401
    from guardbot.bot.states import BulkImportFlow

    user_id = 807

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

    def database_must_not_open():
        raise AssertionError("group secret input must be rejected before database")

    monkeypatch.setattr(runtime, "AsyncSessionLocal", database_must_not_open)
    monkeypatch.setattr(bulk_import, "AsyncSessionLocal", database_must_not_open)

    async def scenario() -> None:
        bot = FakeBot()
        state = runtime.dp.fsm.get_context(
            bot=bot,
            chat_id=user_id,
            user_id=user_id,
        )
        await state.set_state(BulkImportFlow.waiting_credentials)
        await state.set_data({})
        try:
            await runtime.dp.feed_update(
                bot,
                Update(
                    update_id=1,
                    message=Message(
                        message_id=1,
                        date=datetime.now(UTC),
                        chat=Chat(id=-100123, type="group"),
                        from_user=User(
                            id=user_id,
                            is_bot=False,
                            first_name="Test",
                        ),
                        text="login:top-secret",
                    ),
                ),
            )

            assert await state.get_state() == BulkImportFlow.waiting_credentials.state
            assert await state.get_data() == {}
            assert sum(isinstance(call, DeleteMessage) for call in bot.calls) == 1
            feedback = [
                call.text for call in bot.calls if isinstance(call, SendMessage)
            ]
            assert feedback == [
                "❌ Секретные данные можно вводить только в личном чате с ботом."
            ]
        finally:
            await state.clear()
            await bot.session.close()

    asyncio.run(scenario())


def test_bulk_command_prompt_failure_rolls_back_both_stores() -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import common

    user_id = 808
    state = FakeState()
    runtime.user_states[user_id] = {"state": "waiting_mafile"}

    class Message(PromptMessage):
        async def answer(self, *_args, **_kwargs) -> None:
            raise RuntimeError("prompt failed")

    async def scenario() -> None:
        try:
            try:
                await common.cmd_bulk_import(Message(state, user_id), state)
            except RuntimeError as error:
                assert str(error) == "prompt failed"
            else:
                raise AssertionError("prompt failure was not propagated")

            assert state.state is None
            assert state.data == {}
            assert user_id not in runtime.user_states
        finally:
            await runtime.clear_legacy_user_state(user_id)

    asyncio.run(scenario())


def test_bulk_callback_prompt_failure_rolls_back_both_stores(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import common

    user_id = 809
    state = FakeState()
    runtime.user_states[user_id] = {"state": "waiting_mafile"}

    class Message(PromptMessage):
        async def edit_text(self, *_args, **_kwargs) -> None:
            raise RuntimeError("prompt failed")

    async def feedback(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(common, "callback_feedback", feedback)
    message = Message(state, user_id)
    callback = SimpleNamespace(from_user=SimpleNamespace(id=user_id), message=message)

    async def scenario() -> None:
        try:
            try:
                await common.callback_bulk_import_menu(callback, state)
            except RuntimeError as error:
                assert str(error) == "prompt failed"
            else:
                raise AssertionError("prompt failure was not propagated")

            assert state.state is None
            assert state.data == {}
            assert user_id not in runtime.user_states
        finally:
            await runtime.clear_legacy_user_state(user_id)

    asyncio.run(scenario())


def test_bulk_non_text_fallback_precedes_legacy_catch_all() -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers.bulk_import import reject_bulk_import_non_text
    from guardbot.bot.handlers.messages import handle_messages

    callbacks = [handler.callback for handler in runtime.dp.message.handlers]

    assert callbacks.index(reject_bulk_import_non_text) < callbacks.index(handle_messages)
