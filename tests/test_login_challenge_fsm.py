"""Password-login Steam challenges (email / mobile) run on the typed FSM."""

import asyncio
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
BOT_ID = 7_000_000_001


class Client:
    def __init__(self, confirm_type=None) -> None:
        self.closed = False
        self.close_calls = 0
        self.shared_secret = None
        self.steamid = 76561198000000000
        self.confirm_type = confirm_type
        self.confirmed_codes: list[str] = []

    async def login(self):
        return self.confirm_type

    async def confirm_login(self, code: str | None = None) -> None:
        self.confirmed_codes.append(code)

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


class FakeState:
    def __init__(self) -> None:
        self.raw_state = None
        self.data: dict = {}

    async def set_state(self, value) -> None:
        self.raw_state = value.state if hasattr(value, "state") else value

    async def set_data(self, value: dict) -> None:
        self.data = dict(value)

    async def get_state(self):
        return self.raw_state

    async def get_data(self) -> dict:
        return dict(self.data)

    async def clear(self) -> None:
        self.raw_state = None
        self.data = {}


class Status:
    def __init__(self, fail_first_edit: BaseException | None = None) -> None:
        self.edits: list[tuple[str, dict]] = []
        self._fail_first_edit = fail_first_edit

    async def edit_text(self, text: str, **kwargs) -> None:
        if self._fail_first_edit is not None:
            error, self._fail_first_edit = self._fail_first_edit, None
            raise error
        self.edits.append((text, kwargs))


class BotAuthoredMessage:
    """``callback.message``: the bot is its author, never the clicking user."""

    from_user = SimpleNamespace(id=BOT_ID)
    chat = SimpleNamespace(type="private")

    def __init__(self, status: Status | None = None) -> None:
        self.status = status or Status()
        self.answers: list[str] = []

    async def answer(self, text: str, **_kwargs):
        self.answers.append(text)
        return self.status


def _callback_datas(markup) -> list[str]:
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


def _patch_client_factory(monkeypatch, accounts, client) -> None:
    async def create(*_args, **_kwargs):
        return client

    monkeypatch.setattr(
        accounts.SteamSessionManager,
        "create_steam_client_from_db",
        create,
    )


def test_email_login_publishes_typed_challenge_for_explicit_user(monkeypatch) -> None:
    from guardbot.bot import pending_challenges, runtime
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 601
    client = Client(accounts.LoginConfirmType.email)
    _patch_client_factory(monkeypatch, accounts, client)
    mafile = SimpleNamespace(id=1, account_name="account", shared_secret=None)

    async def scenario() -> None:
        state = FakeState()
        message = BotAuthoredMessage()
        await accounts.perform_login_full(
            message,
            mafile,
            SimpleNamespace(),
            telegram_id=user_id,
            state=state,
        )
        try:
            data = await state.get_data()
            assert await state.get_state() == SteamChallengeFlow.waiting_code.state
            assert set(data) == {"challenge_id", "mafile_id", "kind"}
            assert data["mafile_id"] == 1
            assert data["kind"] == "email"
            assert (
                pending_challenges.current_pending_challenge_token(user_id)
                == data["challenge_id"]
            )
            assert pending_challenges.current_pending_challenge_token(BOT_ID) is None
            assert client.closed is False
            assert user_id not in runtime.user_states
            assert BOT_ID not in runtime.user_states
            prompt, options = message.status.edits[-1]
            assert "почт" in prompt.lower()
            assert "account_detail_1" in _callback_datas(options["reply_markup"])
        finally:
            await pending_challenges.clear_pending_challenges_for_user(user_id, state)

    asyncio.run(scenario())


def test_mobile_login_without_secret_publishes_mobile_challenge(monkeypatch) -> None:
    from guardbot.bot import pending_challenges, runtime
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 602
    client = Client(accounts.LoginConfirmType.mobile)
    _patch_client_factory(monkeypatch, accounts, client)
    mafile = SimpleNamespace(id=1, account_name="account", shared_secret=None)

    async def scenario() -> None:
        state = FakeState()
        message = BotAuthoredMessage()
        await accounts.perform_login_full(
            message,
            mafile,
            SimpleNamespace(),
            telegram_id=user_id,
            state=state,
        )
        try:
            data = await state.get_data()
            assert await state.get_state() == SteamChallengeFlow.waiting_code.state
            assert data["kind"] == "mobile"
            assert client.closed is False
            assert user_id not in runtime.user_states
            _prompt, options = message.status.edits[-1]
            callback_datas = _callback_datas(options["reply_markup"])
            assert "import_mafile_for_1" in callback_datas
            assert "account_detail_1" in callback_datas
            # The bot is already waiting for the code; a second Steam login
            # through the manual-code button would only replace this one.
            assert "manual_code_1" not in callback_datas
        finally:
            await pending_challenges.clear_pending_challenges_for_user(user_id, state)

    asyncio.run(scenario())


def test_login_prompt_failure_after_publish_clears_challenge(monkeypatch) -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import accounts

    user_id = 603
    client = Client(accounts.LoginConfirmType.email)
    _patch_client_factory(monkeypatch, accounts, client)
    mafile = SimpleNamespace(id=1, account_name="account", shared_secret=None)

    async def scenario() -> None:
        state = FakeState()
        message = BotAuthoredMessage(Status(TimeoutError("prompt transport")))
        await accounts.perform_login_full(
            message,
            mafile,
            SimpleNamespace(),
            telegram_id=user_id,
            state=state,
        )
        assert client.close_calls == 1
        assert pending_challenges.pending_challenge_count() == 0
        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(scenario())


def test_login_prompt_cancellation_after_publish_clears_challenge(monkeypatch) -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import accounts

    user_id = 604
    client = Client(accounts.LoginConfirmType.email)
    _patch_client_factory(monkeypatch, accounts, client)
    mafile = SimpleNamespace(id=1, account_name="account", shared_secret=None)

    async def scenario() -> None:
        state = FakeState()
        message = BotAuthoredMessage(Status(asyncio.CancelledError()))
        with pytest.raises(asyncio.CancelledError):
            await accounts.perform_login_full(
                message,
                mafile,
                SimpleNamespace(),
                telegram_id=user_id,
                state=state,
            )
        assert client.close_calls == 1
        assert pending_challenges.pending_challenge_count() == 0
        assert await state.get_state() is None

    asyncio.run(scenario())


def test_login_error_does_not_leak_exception_text(monkeypatch) -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import accounts

    user_id = 605
    client = Client()

    async def failing_login():
        raise RuntimeError("proxy http://user:hunter2@10.0.0.1 refused")

    client.login = failing_login
    _patch_client_factory(monkeypatch, accounts, client)
    mafile = SimpleNamespace(id=1, account_name="account", shared_secret=None)

    async def scenario() -> None:
        state = FakeState()
        message = BotAuthoredMessage()
        await accounts.perform_login_full(
            message,
            mafile,
            SimpleNamespace(),
            telegram_id=user_id,
            state=state,
        )
        rendered = " ".join(text for text, _ in message.status.edits)
        assert "hunter2" not in rendered
        assert "10.0.0.1" not in rendered
        assert client.close_calls == 1
        assert pending_challenges.pending_challenge_count() == 0
        assert await state.get_state() is None

    asyncio.run(scenario())


def _code_message(user_id: int, text: str = "ABCDE"):
    class Message:
        from_user = SimpleNamespace(id=user_id)
        chat = SimpleNamespace(type="private")

        def __init__(self) -> None:
            self.text = text
            self.deleted = 0
            self.answers: list[str] = []
            self.status = Status()

        async def delete(self) -> None:
            self.deleted += 1

        async def answer(self, text: str, **_kwargs):
            self.answers.append(text)
            return self.status

    return Message()


def _patch_successful_save(monkeypatch, accounts, saved: list) -> None:
    mafile = SimpleNamespace(id=42, account_name="account")

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def owned(*_args, **_kwargs):
        return mafile

    async def save(*args, **kwargs) -> bool:
        saved.append(kwargs)
        return True

    monkeypatch.setattr(accounts, "AsyncSessionLocal", Session)
    monkeypatch.setattr(accounts, "get_owned_mafile", owned)
    monkeypatch.setattr(accounts.SteamSessionManager, "save_steam_session_to_db", save)


@pytest.mark.parametrize(
    ("kind", "offers_steam_guard_setup"),
    [("email", True), ("mobile", False), ("manual_mobile", False)],
)
def test_challenge_code_kinds_complete_login(
    monkeypatch,
    kind: str,
    offers_steam_guard_setup: bool,
) -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 610
    client = Client()
    saved: list = []
    _patch_successful_save(monkeypatch, accounts, saved)

    async def scenario() -> None:
        state = FakeState()
        await pending_challenges.publish_pending_challenge(
            user_id=user_id,
            mafile_id=42,
            kind=kind,
            client=client,
            state=state,
            fsm_state=SteamChallengeFlow.waiting_code,
            ttl_seconds=60,
        )
        message = _code_message(user_id)
        await accounts.receive_manual_steam_code(message, state)

        assert message.deleted == 1
        assert client.confirmed_codes == ["ABCDE"]
        assert saved == [{"telegram_id": user_id}]
        assert client.close_calls == 1
        assert pending_challenges.pending_challenge_count() == 0
        assert await state.get_state() is None
        _text, options = message.status.edits[-1]
        callback_datas = _callback_datas(options["reply_markup"])
        assert ("setup_steamguard_42" in callback_datas) is offers_steam_guard_setup
        assert "account_detail_42" in callback_datas

    asyncio.run(scenario())


def test_unknown_challenge_kind_is_rejected_as_stale() -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 611
    client = Client()

    async def scenario() -> None:
        state = FakeState()
        await pending_challenges.publish_pending_challenge(
            user_id=user_id,
            mafile_id=42,
            kind="unexpected",
            client=client,
            state=state,
            fsm_state=SteamChallengeFlow.waiting_code,
            ttl_seconds=60,
        )
        message = _code_message(user_id)
        await accounts.receive_manual_steam_code(message, state)

        assert client.confirmed_codes == []
        assert client.close_calls == 1
        assert pending_challenges.pending_challenge_count() == 0
        assert await state.get_state() is None
        assert "устарел" in message.answers[-1]

    asyncio.run(scenario())


def test_document_during_code_challenge_is_deleted_and_challenge_kept() -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 612
    client = Client()

    class Message:
        from_user = SimpleNamespace(id=user_id)
        chat = SimpleNamespace(type="private")
        text = None
        caption = None
        document = SimpleNamespace(file_name="account.maFile", file_size=100)

        def __init__(self) -> None:
            self.deleted = 0
            self.answers: list[str] = []

        async def delete(self) -> None:
            self.deleted += 1

        async def answer(self, text: str, **_kwargs):
            self.answers.append(text)

    async def scenario() -> None:
        state = FakeState()
        token = await pending_challenges.publish_pending_challenge(
            user_id=user_id,
            mafile_id=42,
            kind="mobile",
            client=client,
            state=state,
            fsm_state=SteamChallengeFlow.waiting_code,
            ttl_seconds=60,
        )
        message = Message()
        try:
            await accounts.reject_challenge_document(message, state)
            assert message.deleted == 1
            assert "Импортировать" in message.answers[-1]
            assert client.closed is False
            assert await state.get_state() == SteamChallengeFlow.waiting_code.state
            assert (await state.get_data())["challenge_id"] == token
        finally:
            await pending_challenges.clear_pending_challenges_for_user(user_id, state)

    asyncio.run(scenario())


def test_callback_login_passes_clicking_user_and_state(monkeypatch) -> None:
    from guardbot.bot.handlers import accounts

    calls: list[dict] = []

    class Session:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *_args):
            return False

    class Callback:
        data = "action_login_1"
        from_user = SimpleNamespace(id=613)
        message = BotAuthoredMessage()

        async def answer(self, *_args, **_kwargs) -> None:
            return None

    mafile = SimpleNamespace(id=1, account_name="account", password="encrypted")

    async def proxy(*_args, **_kwargs):
        return True, "proxy"

    async def owned(*_args, **_kwargs):
        return mafile

    async def login(*args, **kwargs) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(accounts, "AsyncSessionLocal", Session)
    monkeypatch.setattr(accounts, "check_proxy_available", proxy)
    monkeypatch.setattr(accounts, "get_owned_mafile", owned)
    monkeypatch.setattr(accounts, "perform_login_full", login)

    state = FakeState()
    asyncio.run(accounts.callback_action_login(Callback(), state))

    assert len(calls) == 1
    assert calls[0]["telegram_id"] == 613
    assert calls[0]["state"] is state


def test_legacy_login_challenge_helpers_and_writers_are_removed() -> None:
    from guardbot.bot.handlers import bulk_import

    for name in (
        "process_steam_code",
        "handle_mafile_for_account",
        "process_mafile_for_account",
        "process_mafile_during_login",
    ):
        assert not hasattr(bulk_import, name), name

    handlers = ROOT / "guardbot" / "bot" / "handlers"
    for path in handlers.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                target = node.func
                name = getattr(target, "id", None) or getattr(target, "attr", None)
                assert name != "set_legacy_user_state", path.name
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value not in {
                    "waiting_steam_code",
                    "waiting_email_code",
                    "waiting_steam_code_or_mafile",
                }, path.name


def test_challenge_document_handler_is_registered_before_catch_all() -> None:
    from datetime import UTC, datetime

    from aiogram.types import Chat, Document, Message, User

    from guardbot.bot.handlers import accounts, messages
    from guardbot.bot.states import SteamChallengeFlow

    def make(message_id: int, **content) -> Message:
        return Message(
            message_id=message_id,
            date=datetime.now(UTC),
            chat=Chat(id=614, type="private"),
            from_user=User(id=614, is_bot=False, first_name="Test"),
            **content,
        )

    document = make(
        1,
        document=Document(
            file_id="f",
            file_unique_id="u",
            file_name="account.maFile",
            file_size=100,
        ),
    )
    code = make(2, text="ABCDE")
    handlers = accounts.dp.message.handlers

    def index_of(callback) -> int:
        return next(
            index
            for index, handler in enumerate(handlers)
            if handler.callback is callback
        )

    document_index = index_of(accounts.reject_challenge_document)
    code_index = index_of(accounts.receive_manual_steam_code)
    catch_all_index = index_of(messages.handle_messages)
    raw_state = SteamChallengeFlow.waiting_code.state

    async def scenario() -> None:
        document_handler = handlers[document_index]
        code_handler = handlers[code_index]
        assert (await document_handler.check(document, raw_state=raw_state))[0] is True
        assert (await document_handler.check(code, raw_state=raw_state))[0] is False
        assert (await code_handler.check(document, raw_state=raw_state))[0] is False
        assert (await document_handler.check(document, raw_state=None))[0] is False
        assert document_index < catch_all_index

    asyncio.run(scenario())


def test_login_command_handler_receives_fsm_context_through_aiogram(monkeypatch) -> None:
    from datetime import UTC, datetime

    from aiogram.filters import CommandObject
    from aiogram.types import Chat, Message, User

    from guardbot.bot.handlers import accounts

    calls: list[dict] = []
    mafile = SimpleNamespace(id=1, account_name="account")

    class Result:
        def scalars(self):
            return SimpleNamespace(all=lambda: [mafile])

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, *_args, **_kwargs):
            return Result()

    async def proxy(*_args, **_kwargs):
        return True, "proxy"

    async def login(*_args, **kwargs) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(accounts, "AsyncSessionLocal", Session)
    from guardbot.bot import runtime

    monkeypatch.setattr(accounts, "check_proxy_available", proxy)
    monkeypatch.setattr(runtime, "check_proxy_available", proxy)
    monkeypatch.setattr(accounts, "perform_login_full", login)

    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=615, type="private"),
        from_user=User(id=615, is_bot=False, first_name="Test"),
        text="/login account",
    )
    handler = next(
        handler
        for handler in accounts.dp.message.handlers
        if getattr(handler.callback, "__name__", "") == "wrapper"
        and handler.callback.__code__.co_freevars
        and any(
            getattr(cell.cell_contents, "__name__", "") == "cmd_login"
            for cell in handler.callback.__closure__ or ()
        )
    )
    state = FakeState()

    asyncio.run(
        handler.call(
            message,
            command=CommandObject(prefix="/", command="login", args="account"),
            state=state,
            raw_state=None,
            bot=SimpleNamespace(),
        )
    )

    assert len(calls) == 1
    assert calls[0]["telegram_id"] == 615
    assert calls[0]["state"] is state


def test_mobile_challenge_success_does_not_claim_authenticator_inactive(
    monkeypatch,
) -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 616
    client = Client()
    saved: list = []
    _patch_successful_save(monkeypatch, accounts, saved)

    async def scenario() -> None:
        state = FakeState()
        await pending_challenges.publish_pending_challenge(
            user_id=user_id,
            mafile_id=42,
            kind="mobile",
            client=client,
            state=state,
            fsm_state=SteamChallengeFlow.waiting_code,
            ttl_seconds=60,
        )
        message = _code_message(user_id)
        await accounts.receive_manual_steam_code(message, state)

        text, options = message.status.edits[-1]
        assert "не активирован" not in text
        assert "import_mafile_for_42" in _callback_datas(options["reply_markup"])

    asyncio.run(scenario())


def test_login_reports_missing_proxy_instead_of_hanging_status(monkeypatch) -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import accounts

    user_id = 617

    async def create(*_args, **_kwargs):
        raise accounts.ProxyRequiredError("proxy required")

    monkeypatch.setattr(
        accounts.SteamSessionManager,
        "create_steam_client_from_db",
        create,
    )
    mafile = SimpleNamespace(id=1, account_name="account", shared_secret=None)

    async def scenario() -> None:
        state = FakeState()
        message = BotAuthoredMessage()
        await accounts.perform_login_full(
            message,
            mafile,
            SimpleNamespace(),
            telegram_id=user_id,
            state=state,
        )
        assert message.status.edits
        assert "прокси" in message.status.edits[-1][0].lower()
        assert pending_challenges.current_pending_challenge_token(user_id) is None
        assert await state.get_state() is None

    asyncio.run(scenario())
