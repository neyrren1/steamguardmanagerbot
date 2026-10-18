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


class Session:
    def __init__(self) -> None:
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def commit(self) -> None:
        self.committed = True


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
    def __init__(
        self,
        state: FSMContext,
        data: str,
        error: BaseException | None = None,
    ) -> None:
        self.from_user = SimpleNamespace(id=USER_ID)
        self.data = data
        self.message = PromptMessage(state, error)
        self._guardbot_acknowledged = True


@pytest.mark.parametrize(
    "handler_name",
    ["callback_change_unique_proxy", "callback_set_unique_proxy"],
)
def test_proxy_callbacks_start_typed_flow_before_prompt(
    monkeypatch,
    handler_name: str,
) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import ProxyFlow

    mafile = SimpleNamespace(
        id=42,
        account_name="account <unsafe>",
        proxy=None,
    )

    async def owned(*_args, **_kwargs):
        return mafile

    async def feedback(*_args, **_kwargs):
        return None

    monkeypatch.setattr(accounts, "AsyncSessionLocal", Session)
    monkeypatch.setattr(accounts, "get_owned_mafile", owned)
    monkeypatch.setattr(accounts, "callback_feedback", feedback)

    async def scenario() -> None:
        state = _state()
        await state.update_data(stale=True)
        runtime.user_states[USER_ID] = {"state": "waiting_steam_code_manual"}
        callback = Callback(state, f"{handler_name.removeprefix('callback_')}_42")

        try:
            await getattr(accounts, handler_name)(callback, state)

            assert USER_ID not in runtime.user_states
            assert await state.get_state() == ProxyFlow.waiting_proxy.state
            assert await state.get_data() == {"mafile_id": 42}
            assert callback.message.snapshots == [
                (ProxyFlow.waiting_proxy.state, {"mafile_id": 42})
            ]
        finally:
            await runtime.clear_user_workflows(USER_ID, state)

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [RuntimeError("failed"), asyncio.CancelledError()])
def test_proxy_prompt_failure_rolls_back(monkeypatch, error: BaseException) -> None:
    from guardbot.bot.handlers import accounts

    async def owned(*_args, **_kwargs):
        return SimpleNamespace(id=42, account_name="account", proxy=None)

    async def feedback(*_args, **_kwargs):
        return None

    monkeypatch.setattr(accounts, "AsyncSessionLocal", Session)
    monkeypatch.setattr(accounts, "get_owned_mafile", owned)
    monkeypatch.setattr(accounts, "callback_feedback", feedback)

    async def scenario() -> None:
        state = _state()
        callback = Callback(state, "change_unique_proxy_42", error)

        with pytest.raises(type(error)):
            await accounts.callback_change_unique_proxy(callback, state)

        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(scenario())


class InputMessage:
    chat = SimpleNamespace(type="private")

    def __init__(self, state: FSMContext, text: str) -> None:
        self.state = state
        self.text = text
        self.from_user = SimpleNamespace(id=USER_ID)
        self.deleted = False
        self.answers = []

    async def delete(self) -> None:
        self.deleted = True

    async def answer(self, text: str, **kwargs):
        self.answers.append(
            (text, kwargs, await self.state.get_state(), await self.state.get_data())
        )
        return SimpleNamespace()


def test_proxy_input_encrypts_and_clears_after_commit(monkeypatch) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import ProxyFlow

    mafile = SimpleNamespace(
        id=42,
        account_name="account",
        proxy=None,
        unique_proxy=False,
    )
    session = Session()
    settings_calls = []

    async def owned(*_args, **_kwargs):
        return mafile

    async def update_settings(*args):
        settings_calls.append(args)

    monkeypatch.setattr(accounts, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(accounts, "get_owned_mafile", owned)
    monkeypatch.setattr(accounts, "parse_proxy_string", lambda value: value)
    monkeypatch.setattr(accounts, "encrypt_value", lambda value: f"enc:{value}")
    monkeypatch.setattr(accounts, "redact_proxy_url", lambda _value: "[REDACTED]")
    monkeypatch.setattr(accounts, "update_account_settings_message", update_settings)

    async def scenario() -> None:
        state = _state()
        await state.set_state(ProxyFlow.waiting_proxy)
        await state.set_data({"mafile_id": 42})
        message = InputMessage(state, "http://user:pass@example.com:8080")

        await accounts.receive_unique_proxy(message, state)

        assert message.deleted is True
        assert session.committed is True
        assert mafile.proxy == "enc:http://user:pass@example.com:8080"
        assert await state.get_state() is None
        assert await state.get_data() == {}
        assert settings_calls

    asyncio.run(scenario())


def test_invalid_proxy_stays_retryable_without_commit(monkeypatch) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import ProxyFlow

    mafile = SimpleNamespace(
        id=42,
        account_name="account",
        proxy=None,
        unique_proxy=False,
    )
    session = Session()

    async def owned(*_args, **_kwargs):
        return mafile

    def invalid_proxy(_value):
        raise ValueError("invalid")

    monkeypatch.setattr(accounts, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(accounts, "get_owned_mafile", owned)
    monkeypatch.setattr(accounts, "parse_proxy_string", invalid_proxy)

    async def scenario() -> None:
        state = _state()
        await state.set_state(ProxyFlow.waiting_proxy)
        await state.set_data({"mafile_id": 42})
        message = InputMessage(state, "bad proxy")

        await accounts.receive_unique_proxy(message, state)

        assert message.deleted is True
        assert session.committed is False
        assert await state.get_state() == ProxyFlow.waiting_proxy.state
        assert await state.get_data() == {"mafile_id": 42}
        retry_markup = message.answers[-1][1]["reply_markup"]
        assert (
            retry_markup.inline_keyboard[1][0].callback_data
            == "cancel_state_to_settings_42"
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "data",
    [
        "change_unique_proxy_",
        "change_unique_proxy_0",
        "change_unique_proxy_-1",
        "change_unique_proxy_42_extra",
        "change_unique_proxy_²",
    ],
)
def test_malformed_proxy_callback_is_rejected_before_database(
    monkeypatch,
    data: str,
) -> None:
    from guardbot.bot.handlers import accounts

    async def feedback(*_args, **_kwargs):
        return None

    monkeypatch.setattr(accounts, "callback_feedback", feedback)
    monkeypatch.setattr(
        accounts,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not open")),
    )

    async def scenario() -> None:
        state = _state()
        callback = Callback(state, data)
        await accounts.callback_change_unique_proxy(callback, state)
        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "data",
    [
        "cancel_state_to_settings_",
        "cancel_state_to_settings_0",
        "cancel_state_to_settings_-1",
        "cancel_state_to_settings_²",
        "cancel_state_to_settings_42_extra",
        "cancel_state_to_settings_2147483648",
    ],
)
def test_malformed_proxy_cancel_clears_before_database(
    monkeypatch,
    data: str,
) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import ProxyFlow

    async def feedback(*_args, **_kwargs):
        return None

    monkeypatch.setattr(accounts, "callback_feedback", feedback)
    monkeypatch.setattr(
        accounts,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not open")),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(ProxyFlow.waiting_proxy)
        await state.set_data({"mafile_id": 42})
        callback = Callback(state, data)

        await accounts.callback_cancel_state_to_settings(callback, state)

        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(scenario())


def test_proxy_cancel_accepts_max_database_integer_for_owner_lookup(monkeypatch) -> None:
    from guardbot.bot.handlers import accounts

    looked_up: list[tuple[int, int]] = []

    async def owned(_session, mafile_id: int, user_id: int):
        looked_up.append((mafile_id, user_id))
        return None

    async def feedback(*_args, **_kwargs):
        return None

    monkeypatch.setattr(accounts, "AsyncSessionLocal", Session)
    monkeypatch.setattr(accounts, "get_owned_mafile", owned)
    monkeypatch.setattr(accounts, "callback_feedback", feedback)

    async def scenario() -> None:
        state = _state()
        callback = Callback(state, "cancel_state_to_settings_2147483647")

        await accounts.callback_cancel_state_to_settings(callback, state)

        assert looked_up == [(2_147_483_647, USER_ID)]
        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(scenario())


def test_account_settings_escapes_redacted_proxy(monkeypatch) -> None:
    from guardbot.bot.handlers import accounts

    mafile = SimpleNamespace(
        id=42,
        telegram_id=USER_ID,
        account_name="account",
        unique_proxy=True,
        proxy="encrypted",
        access_token=None,
        token_expires_at=None,
        notes=None,
        password=None,
        refresh_token=None,
        is_pinned=False,
        group_id=None,
    )

    class Result:
        class Scalars:
            @staticmethod
            def all():
                return [mafile]

        @staticmethod
        def scalars():
            return Result.Scalars()

    class SettingsSession:
        async def execute(self, _statement):
            return Result()

    class SettingsMessage:
        def __init__(self) -> None:
            self.text = None

        async def edit_text(self, text: str, **_kwargs) -> None:
            self.text = text

    monkeypatch.setattr(accounts, "decrypt_value", lambda _value: "proxy")
    monkeypatch.setattr(
        accounts,
        "redact_proxy_url",
        lambda _value: "http://<b>:***@example.com:8080",
    )

    async def scenario() -> None:
        message = SettingsMessage()
        await accounts.update_account_settings_message(
            message,
            mafile,
            SettingsSession(),
        )

        assert "http://&lt;b&gt;:***@example.com:8080" in message.text
        assert "http://<b>:***@example.com:8080" not in message.text

    asyncio.run(scenario())


def test_account_detail_escapes_redacted_proxy(monkeypatch) -> None:
    from guardbot.bot.handlers import accounts

    mafile = SimpleNamespace(
        id=42,
        telegram_id=USER_ID,
        account_name="account",
        steamid=None,
        shared_secret=None,
        identity_secret=None,
        access_token=None,
        last_login_at=None,
        token_expires_at=None,
        unique_proxy=True,
        proxy="encrypted",
        notes=None,
        password=None,
        refresh_token=None,
    )

    class Result:
        class Scalars:
            @staticmethod
            def all():
                return [mafile]

        def __init__(self, scalar=None) -> None:
            self.scalar = scalar

        def scalars(self):
            return self.Scalars()

        def scalar_one_or_none(self):
            return self.scalar

    class DetailSession:
        def __init__(self) -> None:
            self.calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            self.calls += 1
            return Result()

    class DetailMessage:
        def __init__(self) -> None:
            self.text = None

        async def edit_text(self, text: str, **_kwargs) -> None:
            self.text = text

    callback = SimpleNamespace(
        data="account_detail_42",
        from_user=SimpleNamespace(id=USER_ID),
        message=DetailMessage(),
        _guardbot_acknowledged=True,
    )

    async def owned(*_args, **_kwargs):
        return mafile

    async def feedback(*_args, **_kwargs):
        return None

    monkeypatch.setattr(accounts, "AsyncSessionLocal", DetailSession)
    monkeypatch.setattr(accounts, "get_owned_mafile", owned)
    monkeypatch.setattr(accounts, "callback_feedback", feedback)
    monkeypatch.setattr(accounts, "decrypt_value", lambda value: "proxy" if value else None)
    monkeypatch.setattr(
        accounts,
        "redact_proxy_url",
        lambda _value: "http://<b>:***@example.com:8080",
    )

    async def scenario() -> None:
        await accounts.callback_account_detail(callback)

        assert "http://&lt;b&gt;:***@example.com:8080" in callback.message.text
        assert "http://<b>:***@example.com:8080" not in callback.message.text

    asyncio.run(scenario())


def test_proxy_test_result_escapes_all_html_values() -> None:
    from guardbot.bot.handlers import accounts

    source = inspect.getsource(accounts.callback_test_account_proxy)
    assert "escape(mafile.account_name)" in source
    assert "escape(redact_proxy_url(proxy))" in source
    assert "escape(external_ip)" in source


def test_proxy_deletion_failure_aborts_before_fsm_and_database(monkeypatch) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import ProxyFlow

    async def deletion_failed(_message):
        return False

    monkeypatch.setattr(accounts, "delete_secret_message_or_abort", deletion_failed)
    monkeypatch.setattr(
        accounts,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not open")),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(ProxyFlow.waiting_proxy)
        await state.set_data({"mafile_id": 42})
        message = InputMessage(state, "http://user:pass@example.com:8080")
        original_get_data = state.get_data

        async def forbidden_get_data():
            raise AssertionError("FSM data must not be read")

        state.get_data = forbidden_get_data
        try:
            await accounts.receive_unique_proxy(message, state)
        finally:
            state.get_data = original_get_data

        assert await state.get_state() == ProxyFlow.waiting_proxy.state

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fsm_data",
    [
        {},
        {"mafile_id": True},
        {"mafile_id": 0},
        {"mafile_id": 2_147_483_648},
        {"mafile_id": 42, "proxy": "secret"},
    ],
)
def test_malformed_proxy_fsm_data_clears_before_database(
    monkeypatch,
    fsm_data: dict,
) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import ProxyFlow

    monkeypatch.setattr(
        accounts,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not open")),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(ProxyFlow.waiting_proxy)
        await state.set_data(fsm_data)
        message = InputMessage(state, "http://user:pass@example.com:8080")

        await accounts.receive_unique_proxy(message, state)

        assert message.deleted is True
        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(scenario())


def test_proxy_dot_removes_proxy_and_disables_unique_mode(monkeypatch) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import ProxyFlow

    mafile = SimpleNamespace(
        id=42,
        account_name="account",
        proxy="encrypted",
        unique_proxy=True,
    )
    session = Session()

    async def owned(*_args, **_kwargs):
        return mafile

    async def update_settings(*_args):
        return None

    monkeypatch.setattr(accounts, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(accounts, "get_owned_mafile", owned)
    monkeypatch.setattr(accounts, "update_account_settings_message", update_settings)

    async def scenario() -> None:
        state = _state()
        await state.set_state(ProxyFlow.waiting_proxy)
        await state.set_data({"mafile_id": 42})
        message = InputMessage(state, ".")

        await accounts.receive_unique_proxy(message, state)

        assert message.deleted is True
        assert session.committed is True
        assert mafile.proxy is None
        assert mafile.unique_proxy is False
        assert await state.get_state() is None

    asyncio.run(scenario())


def test_non_private_proxy_input_does_not_read_private_flow(monkeypatch) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import ProxyFlow

    monkeypatch.setattr(
        accounts,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not open")),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(ProxyFlow.waiting_proxy)
        await state.set_data({"mafile_id": 42})
        message = InputMessage(state, "http://user:pass@example.com:8080")
        message.chat = SimpleNamespace(type="group")
        original_get_data = state.get_data

        async def forbidden_get_data():
            raise AssertionError("private FSM data must not be read")

        state.get_data = forbidden_get_data
        try:
            await accounts.receive_unique_proxy(message, state)
        finally:
            state.get_data = original_get_data

        assert message.deleted is False
        assert await state.get_state() == ProxyFlow.waiting_proxy.state

    asyncio.run(scenario())


def test_group_proxy_secret_is_deleted_before_database(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.states import ProxyFlow

    class Message:
        from_user = SimpleNamespace(id=USER_ID)
        chat = SimpleNamespace(type="group")
        text = "http://user:pass@example.com:8080"
        caption = None
        document = None

        def __init__(self) -> None:
            self.deleted = False
            self.answers = []

        async def delete(self) -> None:
            self.deleted = True

        async def answer(self, text: str, **_kwargs) -> None:
            self.answers.append(text)

    monkeypatch.setattr(
        runtime,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not open")),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(ProxyFlow.waiting_proxy)
        await state.set_data({"mafile_id": 42})
        message = Message()
        dispatched = False

        async def handler(*_args):
            nonlocal dispatched
            dispatched = True

        event = SimpleNamespace(
            message=message,
            callback_query=None,
            inline_query=None,
        )
        await runtime.LockCheckMiddleware()(
            handler,
            event,
            {"state": state, "raw_state": ProxyFlow.waiting_proxy.state},
        )

        assert message.deleted is True
        assert message.answers
        assert dispatched is False
        assert await state.get_state() == ProxyFlow.waiting_proxy.state

    asyncio.run(scenario())


def test_settings_navigation_clears_proxy_flow(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import ProxyFlow

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return None

    class NavigationSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            return Result()

    class NavigationMessage:
        chat = SimpleNamespace(type="private")

        async def edit_text(self, *_args, **_kwargs) -> None:
            return None

    class NavigationCallback:
        from_user = SimpleNamespace(id=USER_ID)
        data = "settings"
        message = NavigationMessage()
        _guardbot_acknowledged = True

    async def feedback(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "AsyncSessionLocal", NavigationSession)
    monkeypatch.setattr(accounts, "AsyncSessionLocal", NavigationSession)
    monkeypatch.setattr(accounts, "callback_feedback", feedback)

    async def scenario() -> None:
        state = _state()
        await state.set_state(ProxyFlow.waiting_proxy)
        await state.set_data({"mafile_id": 42})
        callback = NavigationCallback()
        event = SimpleNamespace(
            message=None,
            callback_query=callback,
            inline_query=None,
        )

        async def handler(_event, _data):
            await accounts.callback_settings(callback)

        await runtime.LockCheckMiddleware()(
            handler,
            event,
            {"state": state, "raw_state": ProxyFlow.waiting_proxy.state},
        )

        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(scenario())


def test_proxy_retry_callback_rearms_flow_after_navigation_cleanup(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import ProxyFlow

    mafile = SimpleNamespace(id=42, account_name="account", proxy=None)

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return None

    class RuntimeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            return Result()

    async def owned(*_args, **_kwargs):
        return mafile

    async def feedback(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "AsyncSessionLocal", RuntimeSession)
    monkeypatch.setattr(accounts, "AsyncSessionLocal", Session)
    monkeypatch.setattr(accounts, "get_owned_mafile", owned)
    monkeypatch.setattr(accounts, "callback_feedback", feedback)

    async def scenario() -> None:
        state = _state()
        await state.set_state(ProxyFlow.waiting_proxy)
        await state.set_data({"mafile_id": 42})
        callback = Callback(state, "change_unique_proxy_42")
        event = SimpleNamespace(
            message=None,
            callback_query=callback,
            inline_query=None,
        )

        async def handler(_event, _data):
            assert await state.get_state() is None
            await accounts.callback_change_unique_proxy(callback, state)

        await runtime.LockCheckMiddleware()(
            handler,
            event,
            {"state": state, "raw_state": ProxyFlow.waiting_proxy.state},
        )

        assert await state.get_state() == ProxyFlow.waiting_proxy.state
        assert await state.get_data() == {"mafile_id": 42}

    asyncio.run(scenario())


def test_unique_proxy_no_longer_uses_legacy_dispatch() -> None:
    from guardbot.bot.handlers import accounts, messages

    sources = "\n".join(
        [
            inspect.getsource(accounts.callback_change_unique_proxy),
            inspect.getsource(accounts.callback_set_unique_proxy),
            inspect.getsource(messages.handle_messages),
        ]
    )
    assert '"waiting_unique_proxy"' not in sources
