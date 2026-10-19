import asyncio
import io
import json
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


class Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


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


def test_account_mafile_callback_starts_typed_flow_before_prompt(monkeypatch) -> None:
    from guardbot.bot import pending_challenges, runtime
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import AccountMaFileImportFlow

    mafile = SimpleNamespace(id=42, account_name="account <unsafe>")

    async def owned(*_args, **_kwargs):
        return mafile

    async def feedback(*_args, **_kwargs):
        return None

    monkeypatch.setattr(accounts, "AsyncSessionLocal", Session)
    monkeypatch.setattr(accounts, "get_owned_mafile", owned)
    monkeypatch.setattr(accounts, "callback_feedback", feedback)

    async def scenario() -> None:
        state = _state()
        client = _ChallengeClient()
        await _publish_challenge(USER_ID, state, client)
        await state.update_data(stale=True)
        callback = Callback(state, "import_mafile_for_42")

        try:
            await accounts.callback_import_mafile_for(callback, state)

            assert client.close_calls == 1
            assert pending_challenges.current_pending_challenge_token(USER_ID) is None
            assert await state.get_state() == AccountMaFileImportFlow.waiting_document.state
            assert await state.get_data() == {"mafile_id": 42}
            assert callback.message.snapshots == [
                (AccountMaFileImportFlow.waiting_document.state, {"mafile_id": 42})
            ]
        finally:
            await runtime.clear_user_workflows(USER_ID, state)

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [RuntimeError("failed"), asyncio.CancelledError()])
def test_account_mafile_prompt_failure_rolls_back(
    monkeypatch,
    error: BaseException,
) -> None:
    from guardbot.bot.handlers import accounts

    async def owned(*_args, **_kwargs):
        return SimpleNamespace(id=42, account_name="account")

    async def feedback(*_args, **_kwargs):
        return None

    monkeypatch.setattr(accounts, "AsyncSessionLocal", Session)
    monkeypatch.setattr(accounts, "get_owned_mafile", owned)
    monkeypatch.setattr(accounts, "callback_feedback", feedback)

    async def scenario() -> None:
        state = _state()
        callback = Callback(state, "import_mafile_for_42", error)

        with pytest.raises(type(error)):
            await accounts.callback_import_mafile_for(callback, state)

        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "data",
    [
        "import_mafile_for_",
        "import_mafile_for_0",
        "import_mafile_for_-1",
        "import_mafile_for_42_extra",
        "import_mafile_for_²",
        "import_mafile_for_2147483648",
    ],
)
def test_malformed_account_mafile_callback_is_rejected_before_database(
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
        await accounts.callback_import_mafile_for(callback, state)
        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(scenario())


class StatusMessage:
    def __init__(self) -> None:
        self.edits = []

    async def edit_text(self, text: str, **kwargs) -> None:
        self.edits.append((text, kwargs))


class DocumentMessage:
    chat = SimpleNamespace(type="private")

    def __init__(
        self,
        state: FSMContext,
        *,
        file_name: str = "account.mafile",
        file_size: int = 512,
        document: bool = True,
    ) -> None:
        self.state = state
        self.from_user = SimpleNamespace(id=USER_ID)
        self.document = (
            SimpleNamespace(
                file_name=file_name,
                file_size=file_size,
                file_id="file-id",
            )
            if document
            else None
        )
        self.text = None
        self.deleted = False
        self.answers = []

    async def delete(self) -> None:
        self.deleted = True

    async def answer(self, text: str, **kwargs):
        status = StatusMessage()
        self.answers.append((text, kwargs, status))
        return status


class ImportSession(Session):
    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True


class DownloadBot:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def get_file(self, _file_id: str):
        return SimpleNamespace(file_path="remote.mafile")

    async def download_file(self, _file_path: str, destination: io.BytesIO) -> None:
        destination.write(self.payload)


def test_account_mafile_document_encrypts_and_clears_without_steam_network(
    monkeypatch,
) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import AccountMaFileImportFlow

    payload = {
        "account_name": "account",
        "shared_secret": "shared",
        "identity_secret": "identity",
        "device_id": "android:device",
        "secret_1": "secret-one",
        "serial_number": "serial",
        "revocation_code": "revoke",
        "token_gid": "gid",
        "uri": "otpauth://value",
        "Session": {
            "AccessToken": "access",
            "RefreshToken": "refresh",
            "SteamID": "76561198000000000",
        },
    }
    mafile = SimpleNamespace(
        id=42,
        account_name="account",
        shared_secret=None,
        identity_secret=None,
        device_id=None,
        secret_1=None,
        serial_number=None,
        revocation_code=None,
        token_gid=None,
        uri=None,
        access_token=None,
        refresh_token=None,
        steamid=None,
        fully_enrolled=False,
        updated_at=None,
    )
    session = ImportSession()

    async def owned(*_args, **_kwargs):
        return mafile

    monkeypatch.setattr(accounts, "bot", DownloadBot(json.dumps(payload).encode()))
    monkeypatch.setattr(accounts, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(accounts, "get_owned_mafile", owned)
    monkeypatch.setattr(
        accounts,
        "encrypt_value",
        lambda value: None if value is None else f"enc:{value}",
    )
    monkeypatch.setattr(
        accounts,
        "AsyncSteamMobile",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("import must not create a Steam network client")
        ),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(AccountMaFileImportFlow.waiting_document)
        await state.set_data({"mafile_id": 42})
        message = DocumentMessage(state)

        await accounts.receive_account_mafile(message, state)

        assert message.deleted is True
        assert session.committed is True
        assert mafile.shared_secret == "enc:shared"
        assert mafile.identity_secret == "enc:identity"
        assert mafile.device_id == "enc:android:device"
        assert mafile.access_token == "enc:access"
        assert mafile.refresh_token == "enc:refresh"
        assert mafile.steamid == 76561198000000000
        assert mafile.fully_enrolled is True
        assert await state.get_state() is None
        assert await state.get_data() == {}
        edits = [
            edit[0]
            for _answer, _answer_kwargs, status in message.answers
            for edit in status.edits
        ]
        assert any("успешно импортирован" in text for text in edits)

    asyncio.run(scenario())


def test_account_mafile_without_session_preserves_existing_tokens(monkeypatch) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import AccountMaFileImportFlow

    payload = {"account_name": "account", "shared_secret": "shared"}
    mafile = SimpleNamespace(
        id=42,
        account_name="account",
        shared_secret=None,
        identity_secret=None,
        device_id=None,
        secret_1=None,
        serial_number=None,
        revocation_code=None,
        token_gid=None,
        uri=None,
        access_token="existing-access",
        refresh_token="existing-refresh",
        steamid=76561198000000000,
        fully_enrolled=False,
        updated_at=None,
    )
    session = ImportSession()

    async def owned(*_args, **_kwargs):
        return mafile

    monkeypatch.setattr(accounts, "bot", DownloadBot(json.dumps(payload).encode()))
    monkeypatch.setattr(accounts, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(accounts, "get_owned_mafile", owned)
    monkeypatch.setattr(
        accounts,
        "encrypt_value",
        lambda value: None if value is None else f"enc:{value}",
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(AccountMaFileImportFlow.waiting_document)
        await state.set_data({"mafile_id": 42})
        await accounts.receive_account_mafile(DocumentMessage(state), state)

        assert mafile.access_token == "existing-access"
        assert mafile.refresh_token == "existing-refresh"
        assert mafile.steamid == 76561198000000000

    asyncio.run(scenario())


def test_account_mafile_accepts_numeric_steamid() -> None:
    from guardbot.bot.handlers import accounts

    payload = json.dumps(
        {
            "account_name": "account",
            "shared_secret": "shared",
            "Session": {"SteamID": 76561198000000000},
        }
    ).encode()

    _values, session_values = accounts._validated_account_mafile(payload)

    assert session_values["steamid"] == 76561198000000000


@pytest.mark.parametrize(
    "steamid",
    [
        9_223_372_036_854_775_808,
        "9223372036854775808",
        18_446_744_073_709_551_615,
    ],
)
def test_account_mafile_rejects_steamid_outside_postgres_bigint(steamid) -> None:
    from guardbot.bot.handlers import accounts

    payload = json.dumps(
        {
            "account_name": "account",
            "shared_secret": "shared",
            "Session": {"SteamID": steamid},
        }
    ).encode()

    with pytest.raises(ValueError, match="invalid SteamID"):
        accounts._validated_account_mafile(payload)


def test_account_mafile_accepts_max_postgres_bigint_steamid() -> None:
    from guardbot.bot.handlers import accounts

    payload = json.dumps(
        {
            "account_name": "account",
            "shared_secret": "shared",
            "Session": {"SteamID": 9_223_372_036_854_775_807},
        }
    ).encode()

    _values, session_values = accounts._validated_account_mafile(payload)

    assert session_values["steamid"] == 9_223_372_036_854_775_807


def test_account_mafile_deletion_failure_aborts_before_fsm_and_database(
    monkeypatch,
) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import AccountMaFileImportFlow

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
        await state.set_state(AccountMaFileImportFlow.waiting_document)
        await state.set_data({"mafile_id": 42})
        message = DocumentMessage(state)
        original_get_data = state.get_data

        async def forbidden_get_data():
            raise AssertionError("FSM data must not be read")

        state.get_data = forbidden_get_data
        try:
            await accounts.receive_account_mafile(message, state)
        finally:
            state.get_data = original_get_data

        assert await state.get_state() == AccountMaFileImportFlow.waiting_document.state

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fsm_data",
    [
        {},
        {"mafile_id": True},
        {"mafile_id": 0},
        {"mafile_id": 2_147_483_648},
        {"mafile_id": 42, "account_name": "secret"},
    ],
)
def test_malformed_account_mafile_fsm_clears_before_database(
    monkeypatch,
    fsm_data: dict,
) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import AccountMaFileImportFlow

    monkeypatch.setattr(
        accounts,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not open")),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(AccountMaFileImportFlow.waiting_document)
        await state.set_data(fsm_data)
        message = DocumentMessage(state)

        await accounts.receive_account_mafile(message, state)

        assert message.deleted is True
        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("file_name", "file_size", "document"),
    [
        ("account.txt", 10, True),
        ("account.mafile", 1_048_577, True),
        ("account.mafile", 10, False),
    ],
)
def test_invalid_account_mafile_input_stays_retryable_without_database(
    monkeypatch,
    file_name: str,
    file_size: int,
    document: bool,
) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import AccountMaFileImportFlow

    monkeypatch.setattr(
        accounts,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not open")),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(AccountMaFileImportFlow.waiting_document)
        await state.set_data({"mafile_id": 42})
        message = DocumentMessage(
            state,
            file_name=file_name,
            file_size=file_size,
            document=document,
        )

        await accounts.receive_account_mafile(message, state)

        assert message.deleted is True
        assert await state.get_state() == AccountMaFileImportFlow.waiting_document.state
        assert await state.get_data() == {"mafile_id": 42}

    asyncio.run(scenario())


def test_group_account_mafile_is_deleted_before_database(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.states import AccountMaFileImportFlow

    class GroupMessage:
        from_user = SimpleNamespace(id=USER_ID)
        chat = SimpleNamespace(type="group")
        text = None
        caption = None
        document = SimpleNamespace(file_name="account.mafile")

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
        await state.set_state(AccountMaFileImportFlow.waiting_document)
        await state.set_data({"mafile_id": 42})
        message = GroupMessage()
        dispatched = False

        async def handler(*_args):
            nonlocal dispatched
            dispatched = True

        await runtime.LockCheckMiddleware()(
            handler,
            SimpleNamespace(
                message=message,
                callback_query=None,
                inline_query=None,
            ),
            {
                "state": state,
                "raw_state": AccountMaFileImportFlow.waiting_document.state,
            },
        )

        assert message.deleted is True
        assert message.answers
        assert dispatched is False
        assert await state.get_state() == AccountMaFileImportFlow.waiting_document.state
        assert await state.get_data() == {"mafile_id": 42}

    asyncio.run(scenario())


def test_account_mafile_flow_no_longer_uses_legacy_dispatch() -> None:
    from guardbot.bot.handlers import accounts, messages

    sources = "\n".join(
        [
            inspect.getsource(accounts.callback_import_mafile_for),
            inspect.getsource(messages.handle_messages),
        ]
    )
    assert '"waiting_mafile_for_account"' not in sources


def test_account_mafile_import_has_no_time_sync_call() -> None:
    from guardbot.bot.handlers import accounts

    source = inspect.getsource(accounts.receive_account_mafile)
    assert "align_time" not in source
    assert "require_proxy" not in source
