from pathlib import Path


class FakeState:
    def __init__(self) -> None:
        self.raw_state = None
        self.data = {"stale": True}

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


class PromptMessage:
    def __init__(self, state: FakeState, user_id: int = 9901) -> None:
        self.state = state
        self.from_user = type("User", (), {"id": user_id})()
        self.chat = type("Chat", (), {"type": "private"})()
        self.document = None
        self.deleted = 0
        self.answers: list[str] = []

    async def delete(self) -> None:
        self.deleted += 1

    async def answer(self, text: str, **_kwargs):
        self.answers.append(text)
        return None


def test_new_mafile_import_uses_typed_state() -> None:
    from guardbot.bot.states import NewMaFileImportFlow

    assert NewMaFileImportFlow.waiting_document.state == (
        "NewMaFileImportFlow:waiting_document"
    )


def test_legacy_waiting_mafile_has_no_production_references() -> None:
    production_root = Path("guardbot")
    matches = []
    for path in production_root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if '"waiting_mafile"' in source or "'waiting_mafile'" in source:
            matches.append(str(path))

    assert matches == []


def test_import_command_starts_empty_typed_flow(monkeypatch) -> None:
    import asyncio

    from guardbot.bot import runtime
    from guardbot.bot.handlers import imports
    from guardbot.bot.states import NewMaFileImportFlow

    user_id = 9901
    state = FakeState()
    message = PromptMessage(state, user_id)
    runtime.user_states[user_id] = {"state": "waiting_steam_code"}

    async def scenario() -> None:
        try:
            await imports.cmd_import_mafile(message, state)
            assert message.deleted == 1
            assert user_id not in runtime.user_states
            assert await state.get_state() == NewMaFileImportFlow.waiting_document.state
            assert await state.get_data() == {}
            assert message.answers
        finally:
            await state.clear()
            await runtime.clear_legacy_user_state(user_id)

    asyncio.run(scenario())


def test_document_deletion_failure_aborts_before_database(monkeypatch) -> None:
    import asyncio

    from guardbot.bot.handlers import imports
    from guardbot.bot.states import NewMaFileImportFlow

    state = FakeState()
    state.raw_state = NewMaFileImportFlow.waiting_document.state
    message = PromptMessage(state)
    message.document = type(
        "Document",
        (),
        {"file_name": "account.mafile", "file_size": 128, "file_id": "file-1"},
    )()

    async def refuse_delete(_message) -> bool:
        return False

    def database_must_not_open():
        raise AssertionError("database opened after secret deletion failure")

    monkeypatch.setattr(imports, "delete_secret_message_or_abort", refuse_delete)
    monkeypatch.setattr(imports, "AsyncSessionLocal", database_must_not_open)

    asyncio.run(imports.receive_new_mafile(message, state))

    assert state.raw_state == NewMaFileImportFlow.waiting_document.state
    assert message.answers == []


def test_new_account_import_encrypts_empty_password_without_steam_network(
    monkeypatch,
) -> None:
    import asyncio
    import io
    import json

    from guardbot.bot.handlers import imports
    from guardbot.bot.states import NewMaFileImportFlow

    payload = json.dumps(
        {
            "account_name": "new-account",
            "shared_secret": "shared-value",
            "identity_secret": "identity-value",
            "Session": {"SteamID": "76561198000000000"},
        }
    ).encode()

    class DocumentMessage(PromptMessage):
        def __init__(self, state: FakeState) -> None:
            super().__init__(state)
            self.document = type(
                "Document",
                (),
                {
                    "file_name": "account.mafile",
                    "file_size": len(payload),
                    "file_id": "file-1",
                },
            )()
            self.status = type(
                "Status",
                (),
                {
                    "edits": [],
                    "edit_text": lambda self, text, **kwargs: _record_edit(
                        self, text, kwargs
                    ),
                },
            )()

        async def answer(self, text: str, **_kwargs):
            self.answers.append(text)
            return self.status

    async def _record_edit(status, text: str, kwargs: dict) -> None:
        status.edits.append((text, kwargs))

    class FakeBot:
        async def get_file(self, file_id: str):
            assert file_id == "file-1"
            return type("TelegramFile", (), {"file_path": "files/account.mafile"})()

        async def download_file(self, file_path: str, destination: io.BytesIO) -> None:
            assert file_path == "files/account.mafile"
            destination.write(payload)

    class Query:
        def where(self, *_args):
            return self

    class Result:
        def __init__(self, value) -> None:
            self.value = value

        def scalar_one_or_none(self):
            return self.value

        def scalar(self):
            return self.value

    class Session:
        instance = None

        def __init__(self) -> None:
            Session.instance = self
            self.execute_count = 0
            self.added = None
            self.committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, _statement):
            self.execute_count += 1
            values = {1: None, 2: None, 3: 0}
            return Result(values[self.execute_count])

        def add(self, value) -> None:
            self.added = value

        async def commit(self) -> None:
            self.committed = True

    class Record:
        telegram_id = 0
        account_name = ""
        sort_order = 0

        def __init__(self, **values) -> None:
            self.__dict__.update(values)
            self.id = 77

    class Func:
        @staticmethod
        def max(_value):
            return object()

    async def allow_delete(_message) -> bool:
        return True

    monkeypatch.setattr(imports, "delete_secret_message_or_abort", allow_delete)
    monkeypatch.setattr(imports, "bot", FakeBot())
    monkeypatch.setattr(imports, "AsyncSessionLocal", Session)
    monkeypatch.setattr(imports, "Mafile", Record)
    monkeypatch.setattr(imports, "select", lambda *_args: Query())
    monkeypatch.setattr(imports, "func", Func())
    monkeypatch.setattr(imports, "encrypt_value", lambda value: f"enc:{value or ''}")

    state = FakeState()
    state.raw_state = NewMaFileImportFlow.waiting_document.state
    message = DocumentMessage(state)

    asyncio.run(imports.receive_new_mafile(message, state))

    assert Session.instance is not None
    assert Session.instance.committed is True
    assert Session.instance.added.password == "enc:"
    assert Session.instance.added.shared_secret == "enc:shared-value"
    assert Session.instance.added.identity_secret == "enc:identity-value"
    assert Session.instance.added.steamid == 76561198000000000
    assert state.raw_state is None
    assert "AsyncSteamMobile" not in Path(imports.__file__).read_text(encoding="utf-8")


def test_import_prompt_failure_rolls_back_both_state_stores() -> None:
    import asyncio

    from guardbot.bot import runtime
    from guardbot.bot.handlers import imports

    user_id = 9902
    state = FakeState()
    runtime.user_states[user_id] = {"state": "waiting_steam_code_manual"}

    class FailingMessage(PromptMessage):
        async def answer(self, text: str, **_kwargs):
            raise RuntimeError("prompt failed")

    async def scenario() -> None:
        try:
            try:
                await imports.cmd_import_mafile(FailingMessage(state, user_id), state)
            except RuntimeError as exc:
                assert str(exc) == "prompt failed"
            else:
                raise AssertionError("prompt failure did not propagate")
            assert await state.get_state() is None
            assert await state.get_data() == {}
            assert user_id not in runtime.user_states
        finally:
            await state.clear()
            await runtime.clear_legacy_user_state(user_id)

    asyncio.run(scenario())


def test_new_mafile_validation_bounds_root_fields_and_steamid() -> None:
    import json

    from guardbot.bot.handlers import imports

    invalid_payloads = [
        b"[]",
        json.dumps({"account_name": "a", "shared_secret": "s", "Session": []}).encode(),
        json.dumps({"account_name": "a", "shared_secret": "s", "steamid": True}).encode(),
        json.dumps(
            {
                "account_name": "a",
                "shared_secret": "s",
                "steamid": "9223372036854775808",
            }
        ).encode(),
        json.dumps(
            {
                "account_name": "a" * 256,
                "shared_secret": "s",
            }
        ).encode(),
        json.dumps(
            {
                "account_name": "bad\u0000name",
                "shared_secret": "s",
            }
        ).encode(),
        json.dumps(
            {
                "account_name": "bad\ud800name",
                "shared_secret": "s",
            }
        ).encode(),
        json.dumps(
            {
                "account_name": "a",
                "shared_secret": "bad\udc00secret",
            }
        ).encode(),
        json.dumps(
            {
                "account_name": "a",
                "shared_secret": "s",
                "Session": {"AccessToken": "bad\ud800token"},
            }
        ).encode(),
    ]

    for payload in invalid_payloads:
        try:
            imports._validated_new_mafile(payload)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid maFile payload was accepted")


def test_new_mafile_state_filter_rejects_text_and_caption_commands() -> None:
    import asyncio
    from types import SimpleNamespace

    from guardbot.bot.handlers import imports

    filter_instance = imports.NonCommandInputFilter()

    async def scenario() -> None:
        assert await filter_instance(SimpleNamespace(text="/accounts", caption=None)) is False
        assert await filter_instance(SimpleNamespace(text=None, caption="/set_proxy x")) is False
        assert await filter_instance(SimpleNamespace(text=None, caption=None)) is True
        assert await filter_instance(SimpleNamespace(text="ordinary", caption=None)) is True

    asyncio.run(scenario())


def test_registered_new_mafile_handlers_do_not_match_commands() -> None:
    import asyncio
    from datetime import UTC, datetime

    from aiogram.types import Chat, Document, Message, User

    from guardbot.bot.handlers import imports
    from guardbot.bot.states import NewMaFileImportFlow

    text_command = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=9905, type="private"),
        from_user=User(id=9905, is_bot=False, first_name="Test"),
        text="/accounts",
    )
    caption_command = Message(
        message_id=2,
        date=datetime.now(UTC),
        chat=Chat(id=9905, type="private"),
        from_user=User(id=9905, is_bot=False, first_name="Test"),
        caption="/set_proxy bad",
        document=Document(
            file_id="file-1",
            file_unique_id="unique-1",
            file_name="account.mafile",
            file_size=10,
        ),
    )
    callbacks = {
        imports.receive_new_mafile,
        imports.reject_new_mafile_non_document,
    }
    handlers = [
        handler
        for handler in imports.dp.message.handlers
        if handler.callback in callbacks
    ]
    assert len(handlers) == 2

    async def scenario() -> None:
        for handler in handlers:
            text_match, _ = await handler.check(
                text_command,
                raw_state=NewMaFileImportFlow.waiting_document.state,
            )
            caption_match, _ = await handler.check(
                caption_command,
                raw_state=NewMaFileImportFlow.waiting_document.state,
            )
            assert text_match is False
            assert caption_match is False

    asyncio.run(scenario())


def test_new_mafile_session_presence_is_explicit() -> None:
    import json

    from guardbot.bot.handlers import imports

    _, absent = imports._validated_new_mafile(
        json.dumps({"account_name": "a", "shared_secret": "s"}).encode()
    )
    _, present = imports._validated_new_mafile(
        json.dumps(
            {
                "account_name": "a",
                "shared_secret": "s",
                "Session": {},
            }
        ).encode()
    )

    assert absent["present"] is False
    assert present["present"] is True
    assert present["access_token"] is None
    assert present["refresh_token"] is None


def test_new_mafile_uses_top_level_steamid_when_session_omits_it() -> None:
    import json

    from guardbot.bot.handlers import imports

    _, session_values = imports._validated_new_mafile(
        json.dumps(
            {
                "account_name": "a",
                "shared_secret": "s",
                "steamid": "76561198000000000",
                "Session": {"SteamID": None},
            }
        ).encode()
    )

    assert session_values["steamid"] == 76561198000000000


def test_group_document_is_rejected_before_delete_or_database(monkeypatch) -> None:
    import asyncio

    from guardbot.bot.handlers import imports
    from guardbot.bot.states import NewMaFileImportFlow

    state = FakeState()
    state.raw_state = NewMaFileImportFlow.waiting_document.state
    message = PromptMessage(state)
    message.chat = type("Chat", (), {"type": "group"})()
    message.document = type(
        "Document",
        (),
        {"file_name": "account.mafile", "file_size": 12, "file_id": "file-1"},
    )()

    async def delete_must_not_run(_message) -> bool:
        raise AssertionError("leaf handler deleted non-private input")

    def database_must_not_open():
        raise AssertionError("database opened for non-private input")

    monkeypatch.setattr(imports, "delete_secret_message_or_abort", delete_must_not_run)
    monkeypatch.setattr(imports, "AsyncSessionLocal", database_must_not_open)

    asyncio.run(imports.receive_new_mafile(message, state))

    assert state.raw_state == NewMaFileImportFlow.waiting_document.state


def test_group_new_mafile_is_deleted_before_database_by_middleware(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace

    from guardbot.bot import runtime
    from guardbot.bot.states import NewMaFileImportFlow

    class GroupMessage:
        from_user = SimpleNamespace(id=9903)
        chat = SimpleNamespace(type="group")
        text = None
        caption = None
        document = SimpleNamespace(file_name="account.mafile")

        def __init__(self) -> None:
            self.deleted = False
            self.answers: list[str] = []

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
        state = FakeState()
        state.raw_state = NewMaFileImportFlow.waiting_document.state
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
                "raw_state": NewMaFileImportFlow.waiting_document.state,
            },
        )

        assert message.deleted is True
        assert message.answers
        assert dispatched is False
        assert state.raw_state == NewMaFileImportFlow.waiting_document.state

    asyncio.run(scenario())


def test_download_buffer_rejects_forged_oversize_payload() -> None:
    from guardbot.bot.handlers import imports

    buffer = imports._BoundedMaFileBuffer()
    try:
        buffer.write(b"x" * (1_048_576 + 1))
    except ValueError:
        pass
    else:
        raise AssertionError("oversized downloaded maFile was accepted")


def test_cross_owner_account_name_conflict_is_controlled_and_retryable(
    monkeypatch,
) -> None:
    import asyncio
    import io
    import json
    from types import SimpleNamespace

    from guardbot.bot.handlers import imports
    from guardbot.bot.states import NewMaFileImportFlow

    payload = json.dumps(
        {"account_name": "already-owned", "shared_secret": "shared"}
    ).encode()

    class Status:
        def __init__(self) -> None:
            self.edits: list[str] = []

        async def edit_text(self, text: str, **_kwargs) -> None:
            self.edits.append(text)

    class Message:
        from_user = SimpleNamespace(id=9904)
        chat = SimpleNamespace(type="private")
        document = SimpleNamespace(
            file_name="account.mafile",
            file_size=len(payload),
            file_id="file-1",
        )

        def __init__(self) -> None:
            self.status = Status()

        async def answer(self, _text: str, **_kwargs):
            return self.status

    class Bot:
        async def get_file(self, _file_id: str):
            return SimpleNamespace(file_path="file-path")

        async def download_file(self, _file_path: str, destination: io.BytesIO) -> None:
            destination.write(payload)

    class Result:
        def __init__(self, value) -> None:
            self.value = value

        def scalar_one_or_none(self):
            return self.value

        def scalar(self):
            return self.value

    foreign = SimpleNamespace(telegram_id=1234)

    class Session:
        instance = None

        def __init__(self) -> None:
            Session.instance = self
            self.results = iter([None, foreign])
            self.committed = False
            self.rolled_back = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            return Result(next(self.results))

        def add(self, _value) -> None:
            raise AssertionError("conflicting account must not be inserted")

        async def commit(self) -> None:
            self.committed = True

        async def rollback(self) -> None:
            self.rolled_back = True

    async def allow_delete(_message) -> bool:
        return True

    monkeypatch.setattr(imports, "delete_secret_message_or_abort", allow_delete)
    monkeypatch.setattr(imports, "bot", Bot())
    monkeypatch.setattr(imports, "AsyncSessionLocal", Session)

    state = FakeState()
    state.raw_state = NewMaFileImportFlow.waiting_document.state
    message = Message()

    asyncio.run(imports.receive_new_mafile(message, state))

    assert Session.instance.committed is False
    assert state.raw_state == NewMaFileImportFlow.waiting_document.state
    assert message.status.edits == [
        "❌ Не удалось импортировать аккаунт. Проверьте имя и повторите попытку."
    ]


def test_unique_constraint_race_rolls_back_and_keeps_import_retryable(
    monkeypatch,
) -> None:
    import asyncio
    import io
    import json
    from types import SimpleNamespace

    from sqlalchemy.exc import IntegrityError

    from guardbot.bot.handlers import imports
    from guardbot.bot.states import NewMaFileImportFlow

    payload = json.dumps(
        {"account_name": "racing-account", "shared_secret": "shared"}
    ).encode()

    class Status:
        def __init__(self) -> None:
            self.edits: list[str] = []

        async def edit_text(self, text: str, **_kwargs) -> None:
            self.edits.append(text)

    class Message:
        from_user = SimpleNamespace(id=9906)
        chat = SimpleNamespace(type="private")
        document = SimpleNamespace(
            file_name="account.mafile",
            file_size=len(payload),
            file_id="file-1",
        )

        def __init__(self) -> None:
            self.status = Status()

        async def answer(self, _text: str, **_kwargs):
            return self.status

    class Bot:
        async def get_file(self, _file_id: str):
            return SimpleNamespace(file_path="file-path")

        async def download_file(self, _file_path: str, destination: io.BytesIO) -> None:
            destination.write(payload)

    class Query:
        def where(self, *_args):
            return self

    class Result:
        def __init__(self, value) -> None:
            self.value = value

        def scalar_one_or_none(self):
            return self.value

        def scalar(self):
            return self.value

    class Record:
        telegram_id = 0
        account_name = ""
        sort_order = 0

        def __init__(self, **values) -> None:
            self.__dict__.update(values)

    class Func:
        @staticmethod
        def max(_value):
            return object()

    class Session:
        instance = None

        def __init__(self) -> None:
            Session.instance = self
            self.results = iter([None, None, 0])
            self.rolled_back = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            return Result(next(self.results))

        def add(self, _value) -> None:
            return None

        async def commit(self) -> None:
            raise IntegrityError("INSERT", {}, RuntimeError("unique conflict"))

        async def rollback(self) -> None:
            self.rolled_back = True

    async def allow_delete(_message) -> bool:
        return True

    monkeypatch.setattr(imports, "delete_secret_message_or_abort", allow_delete)
    monkeypatch.setattr(imports, "bot", Bot())
    monkeypatch.setattr(imports, "AsyncSessionLocal", Session)
    monkeypatch.setattr(imports, "Mafile", Record)
    monkeypatch.setattr(imports, "select", lambda *_args: Query())
    monkeypatch.setattr(imports, "func", Func())
    monkeypatch.setattr(imports, "encrypt_value", lambda value: f"enc:{value or ''}")

    state = FakeState()
    state.raw_state = NewMaFileImportFlow.waiting_document.state
    message = Message()

    asyncio.run(imports.receive_new_mafile(message, state))

    assert Session.instance.rolled_back is True
    assert state.raw_state == NewMaFileImportFlow.waiting_document.state
    assert message.status.edits == [
        "❌ Не удалось импортировать аккаунт. Проверьте имя и повторите попытку."
    ]
