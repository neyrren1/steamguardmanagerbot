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

    async def get_data(self) -> dict:
        return dict(self.data)


class PromptMessage:
    def __init__(self, state: FakeState) -> None:
        self.state = state
        self.chat = SimpleNamespace(type="private")
        self.snapshots: list[tuple[str | None, dict]] = []

    async def edit_text(self, *_args, **_kwargs) -> None:
        self.snapshots.append((self.state.state, dict(self.state.data)))


def test_login_password_flow_has_typed_state() -> None:
    from guardbot.bot import states

    flow = getattr(states, "LoginPasswordFlow", None)

    assert flow is not None
    assert flow.waiting_password.state == "LoginPasswordFlow:waiting_password"


def test_enter_password_callback_starts_typed_flow_before_prompt(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import LoginPasswordFlow

    user_id = 811
    state = FakeState()
    message = PromptMessage(state)
    callback = SimpleNamespace(
        data="enter_password_42",
        from_user=SimpleNamespace(id=user_id),
        message=message,
    )
    mafile = SimpleNamespace(id=42, account_name="account")

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def owned(_session, mafile_id, owner_id):
        assert (mafile_id, owner_id) == (42, user_id)
        return mafile

    async def feedback(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(accounts, "AsyncSessionLocal", Session)
    monkeypatch.setattr(accounts, "get_owned_mafile", owned)
    monkeypatch.setattr(accounts, "callback_feedback", feedback)
    monkeypatch.setattr(
        accounts,
        "check_proxy_available",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("collecting a local password must not require proxy")
        ),
    )
    runtime.user_states[user_id] = {"state": "waiting_email_code"}

    async def scenario() -> None:
        try:
            await accounts.callback_enter_password(callback, state)

            assert user_id not in runtime.user_states
            assert state.state == LoginPasswordFlow.waiting_password.state
            assert state.data == {"mafile_id": 42}
            assert message.snapshots == [
                (LoginPasswordFlow.waiting_password.state, {"mafile_id": 42})
            ]
        finally:
            await runtime.clear_legacy_user_state(user_id)

    asyncio.run(scenario())


def test_enter_password_prompt_failure_rolls_back_both_stores(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import accounts

    user_id = 812
    state = FakeState()

    class Message(PromptMessage):
        async def edit_text(self, *_args, **_kwargs) -> None:
            raise RuntimeError("prompt failed")

    callback = SimpleNamespace(
        data="enter_password_42",
        from_user=SimpleNamespace(id=user_id),
        message=Message(state),
    )
    mafile = SimpleNamespace(id=42, account_name="account")

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def owned(*_args):
        return mafile

    async def feedback(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(accounts, "AsyncSessionLocal", Session)
    monkeypatch.setattr(accounts, "get_owned_mafile", owned)
    monkeypatch.setattr(accounts, "callback_feedback", feedback)
    monkeypatch.setattr(
        accounts,
        "check_proxy_available",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("collecting a local password must not require proxy")
        ),
    )
    runtime.user_states[user_id] = {"state": "waiting_email_code"}

    async def scenario() -> None:
        try:
            try:
                await accounts.callback_enter_password(callback, state)
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


def test_login_password_deletion_failure_aborts_before_state_and_database(
    monkeypatch,
) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import LoginPasswordFlow

    state = FakeState()
    state.state = LoginPasswordFlow.waiting_password.state
    state.data = {"mafile_id": 42}

    class Message:
        chat = SimpleNamespace(type="private")
        from_user = SimpleNamespace(id=813)
        text = "top-secret"

        def __init__(self) -> None:
            self.answers: list[str] = []

        async def delete(self) -> None:
            raise RuntimeError("delete failed")

        async def answer(self, text: str, **_kwargs) -> None:
            self.answers.append(text)

    def database_must_not_open():
        raise AssertionError("database must not open after deletion failure")

    monkeypatch.setattr(accounts, "AsyncSessionLocal", database_must_not_open)
    message = Message()

    asyncio.run(accounts.receive_login_password(message, state))

    assert state.state == LoginPasswordFlow.waiting_password.state
    assert state.data == {"mafile_id": 42}
    assert message.answers == [
        "❌ Не удалось удалить сообщение с секретными данными. "
        "Данные не были обработаны; повторите попытку в личном чате."
    ]


def test_valid_login_password_is_encrypted_then_flow_clears_before_login(
    monkeypatch,
) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import LoginPasswordFlow

    user_id = 814
    state = FakeState()
    state.state = LoginPasswordFlow.waiting_password.state
    state.data = {"mafile_id": 42}
    mafile = SimpleNamespace(id=42, password=None)
    events: list[str] = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def commit(self) -> None:
            events.append("commit")

    class Message:
        chat = SimpleNamespace(type="private")
        from_user = SimpleNamespace(id=user_id)
        text = "top-secret"

        async def delete(self) -> None:
            events.append("delete")

        async def answer(self, *_args, **_kwargs) -> None:
            events.append("feedback")

    async def owned(_session, mafile_id, owner_id):
        assert (mafile_id, owner_id) == (42, user_id)
        events.append("ownership")
        return mafile

    async def login(_message, received_mafile, _session, **_kwargs) -> None:
        assert received_mafile is mafile
        assert state.state is None
        assert state.data == {}
        assert mafile.password == "enc:top-secret"
        events.append("login")

    monkeypatch.setattr(accounts, "AsyncSessionLocal", Session)
    monkeypatch.setattr(accounts, "get_owned_mafile", owned)
    monkeypatch.setattr(accounts, "encrypt_value", lambda value: f"enc:{value}")
    monkeypatch.setattr(accounts, "perform_login_full", login)

    asyncio.run(accounts.receive_login_password(Message(), state))

    assert events == ["delete", "ownership", "commit", "feedback", "login"]
    assert state.state is None
    assert state.data == {}


def test_malformed_login_password_state_clears_before_database(monkeypatch) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import LoginPasswordFlow

    state = FakeState()
    state.state = LoginPasswordFlow.waiting_password.state
    state.data = {"mafile_id": True}

    class Message:
        chat = SimpleNamespace(type="private")
        from_user = SimpleNamespace(id=815)
        text = "top-secret"

        def __init__(self) -> None:
            self.answers: list[str] = []

        async def delete(self) -> None:
            return None

        async def answer(self, text: str, **_kwargs) -> None:
            self.answers.append(text)

    def database_must_not_open():
        raise AssertionError("database must not open for malformed state")

    monkeypatch.setattr(accounts, "AsyncSessionLocal", database_must_not_open)
    message = Message()

    asyncio.run(accounts.receive_login_password(message, state))

    assert state.state is None
    assert state.data == {}
    assert message.answers == ["❌ Сессия устарела. Начните вход заново."]


def test_removed_login_password_legacy_state_has_no_production_references() -> None:
    from pathlib import Path

    production_root = Path(__file__).resolve().parents[1] / "guardbot"
    references = [
        str(path.relative_to(production_root))
        for path in production_root.rglob("*.py")
        if "waiting_password_for_login" in path.read_text(encoding="utf-8")
    ]

    assert references == []


def test_login_password_non_text_fallback_precedes_legacy_catch_all() -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers.accounts import reject_login_password_non_text
    from guardbot.bot.handlers.messages import handle_messages

    callbacks = [handler.callback for handler in runtime.dp.message.handlers]

    assert callbacks.index(reject_login_password_non_text) < callbacks.index(
        handle_messages
    )


def test_dispatcher_rejects_group_login_password_before_database(
    monkeypatch,
) -> None:
    from datetime import UTC, datetime

    from aiogram import Bot
    from aiogram.methods import DeleteMessage, SendMessage
    from aiogram.types import Chat, Message, Update, User

    from guardbot.bot import runtime
    from guardbot.bot.handlers import accounts, messages  # noqa: F401
    from guardbot.bot.states import LoginPasswordFlow

    user_id = 816

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
        raise AssertionError("group password must be rejected before database")

    monkeypatch.setattr(runtime, "AsyncSessionLocal", database_must_not_open)
    monkeypatch.setattr(accounts, "AsyncSessionLocal", database_must_not_open)

    async def scenario() -> None:
        bot = FakeBot()
        state = runtime.dp.fsm.get_context(
            bot=bot,
            chat_id=user_id,
            user_id=user_id,
        )
        await state.set_state(LoginPasswordFlow.waiting_password)
        await state.set_data({"mafile_id": 42})
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
                        text="top-secret",
                    ),
                ),
            )

            assert await state.get_state() == LoginPasswordFlow.waiting_password.state
            assert await state.get_data() == {"mafile_id": 42}
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


def test_dispatcher_routes_private_login_password_through_typed_fsm(
    monkeypatch,
) -> None:
    from datetime import UTC, datetime

    from aiogram import Bot
    from aiogram.methods import DeleteMessage, SendMessage
    from aiogram.types import Chat, Message, Update, User

    from guardbot.bot import runtime
    from guardbot.bot.handlers import accounts, messages  # noqa: F401
    from guardbot.bot.states import LoginPasswordFlow

    user_id = 817
    mafile = SimpleNamespace(id=42, password=None)

    class LockResult:
        @staticmethod
        def scalar_one_or_none():
            return None

    class LockSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            return LockResult()

    class DomainSession:
        def __init__(self) -> None:
            self.committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def commit(self) -> None:
            self.committed = True

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
                    chat=Chat(id=user_id, type="private"),
                    text=method.text,
                )
            raise AssertionError(f"Unexpected Bot API method: {method!r}")

    domain_session = DomainSession()
    login_calls = []

    async def owned(_session, mafile_id, owner_id):
        assert (mafile_id, owner_id) == (42, user_id)
        return mafile

    async def login(_message, received_mafile, received_session, **_kwargs) -> None:
        assert received_mafile is mafile
        assert received_session is domain_session
        login_calls.append(True)

    monkeypatch.setattr(runtime, "AsyncSessionLocal", LockSession)
    monkeypatch.setattr(accounts, "AsyncSessionLocal", lambda: domain_session)
    monkeypatch.setattr(accounts, "get_owned_mafile", owned)
    monkeypatch.setattr(accounts, "encrypt_value", lambda value: f"enc:{value}")
    monkeypatch.setattr(accounts, "perform_login_full", login)

    async def scenario() -> None:
        bot = FakeBot()
        state = runtime.dp.fsm.get_context(
            bot=bot,
            chat_id=user_id,
            user_id=user_id,
        )
        await state.set_state(LoginPasswordFlow.waiting_password)
        await state.set_data({"mafile_id": 42})
        try:
            await runtime.dp.feed_update(
                bot,
                Update(
                    update_id=1,
                    message=Message(
                        message_id=1,
                        date=datetime.now(UTC),
                        chat=Chat(id=user_id, type="private"),
                        from_user=User(
                            id=user_id,
                            is_bot=False,
                            first_name="Test",
                        ),
                        text="top-secret",
                    ),
                ),
            )

            assert domain_session.committed is True
            assert mafile.password == "enc:top-secret"
            assert await state.get_state() is None
            assert await state.get_data() == {}
            assert login_calls == [True]
            assert sum(isinstance(call, DeleteMessage) for call in bot.calls) == 1
        finally:
            await state.clear()
            await bot.session.close()

    asyncio.run(scenario())
