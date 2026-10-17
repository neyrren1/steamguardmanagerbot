import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_all_mafile_constructor_passwords_are_encrypted() -> None:
    handlers = Path("guardbot/bot/handlers")
    violations: list[str] = []

    for source_path in handlers.glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "Mafile":
                continue
            for keyword in node.keywords:
                if keyword.arg != "password":
                    continue
                if isinstance(keyword.value, ast.Constant) and keyword.value.value is None:
                    continue
                if not (
                    isinstance(keyword.value, ast.Call)
                    and isinstance(keyword.value.func, ast.Name)
                    and keyword.value.func.id == "encrypt_value"
                ):
                    violations.append(f"{source_path}:{node.lineno}")

    assert violations == []


def test_pending_mafile_payload_is_not_kept_as_plaintext_state() -> None:
    imports_source = Path("guardbot/bot/handlers/imports.py").read_text(
        encoding="utf-8"
    )
    messages_source = Path("guardbot/bot/handlers/messages.py").read_text(
        encoding="utf-8"
    )

    assert '"mafile_data": mafile_data' not in imports_source
    assert 'state_data.get("mafile_data")' not in messages_source
    assert '"encrypted_mafile_data"' in imports_source
    assert 'state_data.get("encrypted_mafile_data")' in messages_source


def test_device_id_is_not_echoed_back_into_telegram_history() -> None:
    source = Path("guardbot/bot/handlers/messages.py").read_text(encoding="utf-8")
    assert "Device ID: <code>{device_id}</code>" not in source


def test_secret_callbacks_use_central_feedback_instead_of_direct_ack() -> None:
    targets = {
        Path("guardbot/bot/handlers/features.py"): {
            "callback_lock_bot",
            "callback_cancel_password_to_settings",
        },
        Path("guardbot/bot/handlers/commands.py"): {"callback_cancel_unlock"},
        Path("guardbot/bot/handlers/accounts.py"): {
            "callback_cancel_state_to_settings",
            "callback_remove_account_proxy",
        },
        Path("guardbot/bot/handlers/common.py"): {"callback_cancel_state_to_start"},
    }

    for path, function_names in targets.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for function_name in function_names:
            function = functions[function_name]
            answer_lines: list[int] = []
            feedback_lines: list[int] = []
            for node in ast.walk(function):
                if not isinstance(node, ast.Call):
                    continue
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "answer"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "callback"
                ):
                    answer_lines.append(node.lineno)
                if isinstance(node.func, ast.Name) and node.func.id == "callback_feedback":
                    feedback_lines.append(node.lineno)

            assert answer_lines == [], function_name
            assert feedback_lines, function_name
            if function_name == "callback_remove_account_proxy":
                nested_callback_calls = [
                    node
                    for node in ast.walk(function)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "callback_account_settings"
                ]
                assert nested_callback_calls == []
            if function_name == "callback_cancel_password_to_settings":
                nested_callback_calls = [
                    node
                    for node in ast.walk(function)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "callback_settings"
                ]
                assert nested_callback_calls == []


def test_account_callbacks_do_not_acknowledge_directly() -> None:
    tree = ast.parse(
        Path("guardbot/bot/handlers/accounts.py").read_text(encoding="utf-8")
    )
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    function_names = {"callback_test_account_proxy", "callback_confirm_delete"}
    for function_name in function_names:
        answer_calls = [
            node
            for node in ast.walk(functions[function_name])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "answer"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "callback"
        ]
        assert answer_calls == [], function_name


def test_callback_handlers_never_acknowledge_outside_central_runtime() -> None:
    violations: list[str] = []
    for path in Path("guardbot/bot/handlers").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if (
                isinstance(function, ast.Attribute)
                and function.attr == "answer"
                and isinstance(function.value, ast.Name)
                and function.value.id == "callback"
            ):
                violations.append(f"{path}:{node.lineno}")
    assert violations == []


def test_database_engine_does_not_echo_sql_values() -> None:
    tree = ast.parse(Path("guardbot/database.py").read_text(encoding="utf-8"))
    engine_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "create_async_engine"
    ]
    assert engine_calls
    for call in engine_calls:
        echo = next((item.value for item in call.keywords if item.arg == "echo"), None)
        assert isinstance(echo, ast.Constant) and echo.value is False


def test_bulk_credentials_message_is_deleted_before_validation() -> None:
    from guardbot.bot.handlers import bulk_import

    class Message:
        from_user = SimpleNamespace(id=987)
        chat = SimpleNamespace(type="private")
        text = ""

        def __init__(self) -> None:
            self.deleted = False
            self.answers: list[str] = []

        async def delete(self) -> None:
            self.deleted = True

        async def answer(self, text, **kwargs) -> None:
            self.answers.append(text)

    async def scenario() -> None:
        message = Message()
        await bulk_import.process_bulk_import(message, 987)
        assert message.deleted is True
        assert message.answers == ["❌ Отправьте список аккаунтов"]

    asyncio.run(scenario())


def test_proxy_credentials_are_rejected_and_deleted_outside_private_chat(
    monkeypatch,
) -> None:
    from guardbot.bot.handlers import imports

    class Message:
        from_user = SimpleNamespace(id=987)
        chat = SimpleNamespace(type="group")

        def __init__(self) -> None:
            self.deleted = False
            self.answers: list[str] = []

        async def delete(self) -> None:
            self.deleted = True

        async def answer(self, text, **kwargs) -> None:
            self.answers.append(text)

    monkeypatch.setattr(
        imports,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not be opened")),
    )

    async def scenario() -> None:
        message = Message()
        command = SimpleNamespace(
            args="http://user:password@proxy.example.com:8080"
        )
        await imports.cmd_set_proxy(message, command)
        assert message.deleted is True
        assert "личном чате" in message.answers[0]

    asyncio.run(scenario())


def test_invalid_steam_guard_code_keeps_login_state_retryable() -> None:
    from guardbot.bot.handlers import bulk_import

    class Client:
        def __init__(self) -> None:
            self.closed = False

        async def confirm_login(self, code: str) -> None:
            raise bulk_import.InvalidSteamGuardCode()

        async def close(self) -> None:
            self.closed = True

    class Message:
        from_user = SimpleNamespace(id=987)
        chat = SimpleNamespace(type="private")
        text = "ABCDE"

        async def delete(self) -> None:
            return None

        async def answer(self, text, **kwargs):
            return SimpleNamespace()

    async def scenario() -> None:
        client = Client()
        state_data = {
            "state": "waiting_steam_code",
            "account_name": "test-account",
            "client": client,
            "mafile_id": 123,
            "db_session": object(),
        }
        bulk_import.user_states[987] = state_data
        try:
            await bulk_import.process_steam_code(Message(), state_data, 987)
            assert bulk_import.user_states[987] is state_data
            assert client.closed is False
        finally:
            bulk_import.user_states.pop(987, None)

    asyncio.run(scenario())


async def _async_result(value):
    return value


def test_manual_code_login_double_failure_still_closes_client(monkeypatch) -> None:
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageText

    from guardbot.bot.handlers import accounts

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Client:
        def __init__(self) -> None:
            self.close_calls = 0

        async def login(self) -> None:
            raise ValueError("login failed")

        async def close(self) -> None:
            self.close_calls += 1

    class Message:
        async def edit_text(self, *args, **kwargs) -> None:
            raise TelegramBadRequest(
                method=EditMessageText(chat_id=1, message_id=1, text="error"),
                message="edit failed",
            )

    class Callback:
        data = "manual_code_1"
        from_user = SimpleNamespace(id=987)
        message = Message()

        def __init__(self) -> None:
            self.answer_calls = 0

        async def answer(self, *args, **kwargs) -> None:
            self.answer_calls += 1

    client = Client()
    mafile = SimpleNamespace(id=1, account_name="account")
    monkeypatch.setattr(accounts, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        accounts,
        "get_owned_mafile",
        lambda *args, **kwargs: _async_result(mafile),
    )
    monkeypatch.setattr(
        accounts.SteamSessionManager,
        "create_steam_client_from_db",
        lambda *args, **kwargs: _async_result(client),
    )

    async def scenario() -> None:
        callback = Callback()
        try:
            await accounts.callback_manual_code(callback)
        except TelegramBadRequest:
            pass
        assert callback.answer_calls == 1
        assert client.close_calls == 1
        assert 987 not in accounts.user_states

    asyncio.run(scenario())


def test_action_login_acknowledges_before_missing_password_prompt(monkeypatch) -> None:
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageText

    from guardbot.bot.handlers import accounts

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Message:
        async def edit_text(self, *args, **kwargs) -> None:
            raise TelegramBadRequest(
                method=EditMessageText(chat_id=1, message_id=1, text="prompt"),
                message="edit failed",
            )

    class Callback:
        data = "action_login_1"
        from_user = SimpleNamespace(id=987)
        message = Message()

        def __init__(self) -> None:
            self.answer_calls = 0

        async def answer(self, *args, **kwargs) -> None:
            self.answer_calls += 1

    mafile = SimpleNamespace(id=1, account_name="account", password="")
    monkeypatch.setattr(accounts, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        accounts,
        "check_proxy_available",
        lambda *args, **kwargs: _async_result((True, "proxy")),
    )
    monkeypatch.setattr(
        accounts,
        "get_owned_mafile",
        lambda *args, **kwargs: _async_result(mafile),
    )

    async def scenario() -> None:
        callback = Callback()
        try:
            await accounts.callback_action_login(callback)
        except TelegramBadRequest:
            pass
        assert callback.answer_calls == 1

    asyncio.run(scenario())


def test_login_command_is_rejected_before_proxy_lookup_outside_private_chat(
    monkeypatch,
) -> None:
    from guardbot.bot.handlers import accounts

    class Message:
        from_user = SimpleNamespace(id=987)
        chat = SimpleNamespace(type="group")

        def __init__(self) -> None:
            self.deleted = False
            self.answers: list[str] = []

        async def delete(self) -> None:
            self.deleted = True

        async def answer(self, text, **kwargs) -> None:
            self.answers.append(text)

    async def forbidden_proxy_lookup(*args, **kwargs):
        raise AssertionError("proxy/database lookup must not run")

    monkeypatch.setattr(accounts, "check_proxy_available", forbidden_proxy_lookup)

    async def scenario() -> None:
        message = Message()
        await accounts.cmd_login(message, SimpleNamespace(args="account"))
        assert message.deleted is True
        assert "личном чате" in message.answers[0]

    asyncio.run(scenario())


def test_password_login_handoff_preserves_new_challenge_state(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import LoginPasswordFlow

    class Session:
        async def commit(self) -> None:
            return None

    class SessionContext:
        async def __aenter__(self):
            return Session()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class Client:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    class Message:
        from_user = SimpleNamespace(id=987)
        chat = SimpleNamespace(type="private")
        text = "password"

        async def delete(self) -> None:
            return None

        async def answer(self, *args, **kwargs) -> None:
            return None

    mafile = SimpleNamespace(id=1, account_name="account", password=None)
    client = Client()

    class State:
        def __init__(self) -> None:
            self.state = LoginPasswordFlow.waiting_password.state
            self.data = {"mafile_id": 1}

        async def get_data(self):
            return dict(self.data)

        async def clear(self) -> None:
            self.state = None
            self.data = {}

    async def perform_login(*args, **kwargs) -> None:
        runtime.user_states[987] = {
            "state": "waiting_email_code",
            "client": client,
            "mafile_id": 1,
        }

    monkeypatch.setattr(accounts, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        accounts,
        "get_owned_mafile",
        lambda *args, **kwargs: _async_result(mafile),
    )
    monkeypatch.setattr(accounts, "encrypt_value", lambda value: "encrypted")
    monkeypatch.setattr(accounts, "perform_login_full", perform_login)

    async def scenario() -> None:
        try:
            await accounts.receive_login_password(Message(), State())
            assert runtime.user_states[987]["state"] == "waiting_email_code"
            assert client.closed is False
        finally:
            await runtime.clear_legacy_user_state(987)

    asyncio.run(scenario())


def test_login_completion_edit_failure_closes_untransferred_client(monkeypatch) -> None:
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageText

    from guardbot.bot.handlers import accounts

    class Client:
        steamid = 123

        def __init__(self) -> None:
            self.close_calls = 0

        async def login(self):
            return accounts.LoginConfirmType.none

        async def confirm_login(self) -> None:
            return None

        async def get_account_country(self):
            return None

        async def close(self) -> None:
            self.close_calls += 1

    class StatusMessage:
        async def edit_text(self, *args, **kwargs) -> None:
            raise TelegramBadRequest(
                method=EditMessageText(chat_id=1, message_id=1, text="done"),
                message="edit failed",
            )

    class Message:
        from_user = SimpleNamespace(id=987)

        async def answer(self, *args, **kwargs):
            return StatusMessage()

    class Session:
        async def flush(self) -> None:
            return None

    client = Client()
    mafile = SimpleNamespace(id=1, account_name="account")
    monkeypatch.setattr(
        accounts.SteamSessionManager,
        "create_steam_client_from_db",
        lambda *args, **kwargs: _async_result(client),
    )
    monkeypatch.setattr(
        accounts.SteamSessionManager,
        "save_steam_session_to_db",
        lambda *args, **kwargs: _async_result(None),
    )

    async def scenario() -> None:
        try:
            await accounts.perform_login_full(Message(), mafile, Session())
        except TelegramBadRequest:
            pass
        assert client.close_calls == 1

    asyncio.run(scenario())


def test_non_telegram_challenge_prompt_failure_rolls_back_client_state(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import accounts

    class Client:
        def __init__(self) -> None:
            self.close_calls = 0

        async def login(self):
            return accounts.LoginConfirmType.email

        async def close(self) -> None:
            self.close_calls += 1

    class StatusMessage:
        def __init__(self) -> None:
            self.edit_calls = 0

        async def edit_text(self, *args, **kwargs) -> None:
            self.edit_calls += 1
            if self.edit_calls == 1:
                raise TimeoutError("prompt transport timeout")

    class Message:
        from_user = SimpleNamespace(id=988)

        async def answer(self, *args, **kwargs):
            return StatusMessage()

    client = Client()
    mafile = SimpleNamespace(id=1, account_name="account")
    monkeypatch.setattr(
        accounts.SteamSessionManager,
        "create_steam_client_from_db",
        lambda *args, **kwargs: _async_result(client),
    )

    async def scenario() -> None:
        await accounts.perform_login_full(Message(), mafile, SimpleNamespace())
        assert 988 not in runtime.user_states
        assert client.close_calls == 1

    asyncio.run(scenario())


def test_manual_code_cancellation_closes_untransferred_client(monkeypatch) -> None:
    from guardbot.bot.handlers import accounts

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Client:
        def __init__(self) -> None:
            self.login_started = asyncio.Event()
            self.closed = False

        async def login(self):
            self.login_started.set()
            await asyncio.Event().wait()

        async def close(self) -> None:
            self.closed = True

    class Callback:
        data = "manual_code_1"
        from_user = SimpleNamespace(id=989)
        message = SimpleNamespace(edit_text=lambda *args, **kwargs: _async_result(None))

        async def answer(self, *args, **kwargs) -> None:
            return None

    client = Client()
    mafile = SimpleNamespace(id=1, account_name="account")
    monkeypatch.setattr(accounts, "AsyncSessionLocal", Session)
    monkeypatch.setattr(
        accounts,
        "get_owned_mafile",
        lambda *args, **kwargs: _async_result(mafile),
    )
    monkeypatch.setattr(
        accounts.SteamSessionManager,
        "create_steam_client_from_db",
        lambda *args, **kwargs: _async_result(client),
    )

    async def scenario() -> None:
        task = asyncio.create_task(accounts.callback_manual_code(Callback()))
        await client.login_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.closed is True

    asyncio.run(scenario())


def test_manual_code_cancellation_after_state_commit_clears_transferred_client(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import accounts

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Client:
        def __init__(self, *, block_close: bool = False) -> None:
            self.block_close = block_close
            self.close_started = asyncio.Event()
            self.allow_close = asyncio.Event()
            self.closed = False

        async def login(self):
            return accounts.LoginConfirmType.mobile

        async def close(self) -> None:
            self.close_started.set()
            if self.block_close:
                await self.allow_close.wait()
            self.closed = True

    class Callback:
        data = "manual_code_1"
        from_user = SimpleNamespace(id=990)
        message = SimpleNamespace(edit_text=lambda *args, **kwargs: _async_result(None))

        async def answer(self, *args, **kwargs) -> None:
            return None

    old_client = Client(block_close=True)
    new_client = Client()
    runtime.user_states[990] = {
        "state": "waiting_email_code",
        "client": old_client,
    }
    mafile = SimpleNamespace(id=1, account_name="account")
    monkeypatch.setattr(accounts, "AsyncSessionLocal", Session)
    monkeypatch.setattr(
        accounts,
        "get_owned_mafile",
        lambda *args, **kwargs: _async_result(mafile),
    )
    monkeypatch.setattr(
        accounts.SteamSessionManager,
        "create_steam_client_from_db",
        lambda *args, **kwargs: _async_result(new_client),
    )

    async def scenario() -> None:
        task = asyncio.create_task(accounts.callback_manual_code(Callback()))
        await old_client.close_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        old_client.allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert 990 not in runtime.user_states
        assert old_client.closed is True
        assert new_client.closed is True

    asyncio.run(scenario())


def test_manual_code_cancellation_preserves_newer_replacement_state(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import accounts

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Client:
        def __init__(self) -> None:
            self.closed = False

        async def login(self):
            return accounts.LoginConfirmType.mobile

        async def close(self) -> None:
            self.closed = True

    edit_started = asyncio.Event()

    class Message:
        async def edit_text(self, *args, **kwargs) -> None:
            edit_started.set()
            await asyncio.Event().wait()

    class Callback:
        data = "manual_code_1"
        from_user = SimpleNamespace(id=994)
        message = Message()

        async def answer(self, *args, **kwargs) -> None:
            return None

    initial_client = Client()
    challenge_client = Client()
    replacement_client = Client()
    runtime.user_states[994] = {
        "state": "waiting_email_code",
        "client": initial_client,
    }
    monkeypatch.setattr(accounts, "AsyncSessionLocal", Session)
    monkeypatch.setattr(
        accounts,
        "get_owned_mafile",
        lambda *args, **kwargs: _async_result(
            SimpleNamespace(id=1, account_name="account")
        ),
    )
    monkeypatch.setattr(
        accounts.SteamSessionManager,
        "create_steam_client_from_db",
        lambda *args, **kwargs: _async_result(challenge_client),
    )

    async def scenario() -> None:
        task = asyncio.create_task(accounts.callback_manual_code(Callback()))
        await edit_started.wait()
        replacement = {
            "state": "waiting_email_code",
            "client": replacement_client,
        }
        await runtime.set_legacy_user_state(994, replacement, ttl_seconds=60)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        try:
            assert runtime.user_states[994] is replacement
            assert replacement_client.closed is False
        finally:
            await runtime.clear_legacy_user_state(994)

    asyncio.run(scenario())


def test_successful_single_mafile_import_clears_terminal_state(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import imports

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return SimpleNamespace()

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            return Result()

        async def commit(self) -> None:
            return None

    status_edits = []

    class StatusMessage:
        async def edit_text(self, text, **kwargs) -> None:
            status_edits.append((text, kwargs))

    class Message:
        document = SimpleNamespace(
            file_name="account.mafile",
            file_size=100,
            file_id="file-id",
        )

        async def delete(self) -> None:
            return None

        async def answer(self, *args, **kwargs):
            return StatusMessage()

    async def get_file(file_id):
        return SimpleNamespace(file_path="file-path")

    async def download_file(file_path, destination) -> None:
        destination.write(
            b'{"account_name":"x</b><a href=\\"https://example.invalid/\\">click</a><b>",'
            b'"shared_secret":"secret"}'
        )

    monkeypatch.setattr(imports, "AsyncSessionLocal", Session)
    monkeypatch.setattr(imports, "encrypt_value", lambda value: f"enc:{value}")
    monkeypatch.setattr(
        imports,
        "bot",
        SimpleNamespace(get_file=get_file, download_file=download_file),
    )

    async def scenario() -> None:
        runtime.user_states[991] = {"state": "waiting_mafile"}
        await imports.process_single_mafile(Message(), 991)
        assert 991 not in runtime.user_states
        confirmation = status_edits[-1][0]
        assert 'x&lt;/b&gt;&lt;a href=&quot;https://example.invalid/&quot;&gt;' in confirmation
        assert '<a href="https://example.invalid/">' not in confirmation

    asyncio.run(scenario())


def test_confirm_delete_escapes_account_name_after_commit(monkeypatch) -> None:
    from guardbot.bot.handlers import accounts

    hostile_name = 'x</b><a href="https://example.invalid/">click</a><b>'
    mafile = SimpleNamespace(id=42, account_name=hostile_name)

    class Session:
        def __init__(self) -> None:
            self.committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def delete(self, value) -> None:
            assert value is mafile

        async def commit(self) -> None:
            self.committed = True

    session = Session()

    async def owned(*_args, **_kwargs):
        return mafile

    edits = []

    class Message:
        async def edit_text(self, text, **kwargs) -> None:
            assert session.committed is True
            edits.append((text, kwargs))

    callback = SimpleNamespace(
        data="confirm_delete_42",
        from_user=SimpleNamespace(id=991),
        message=Message(),
        _guardbot_acknowledged=True,
    )

    monkeypatch.setattr(accounts, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(accounts, "get_owned_mafile", owned)

    async def scenario() -> None:
        await accounts.callback_confirm_delete(callback)

        confirmation = edits[-1][0]
        assert 'x&lt;/b&gt;&lt;a href=&quot;https://example.invalid/&quot;&gt;' in confirmation
        assert '<a href="https://example.invalid/">' not in confirmation

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("module_name", "handler_name", "command_args"),
    [
        ("commands", "cmd_export_mafile", "account"),
        ("commands", "cmd_export_all", None),
        ("bulk_import", "cmd_export_group", "group"),
    ],
)
def test_secret_export_commands_reject_group_before_database(
    monkeypatch,
    module_name: str,
    handler_name: str,
    command_args: str | None,
) -> None:
    from guardbot.bot.handlers import bulk_import, commands

    module = commands if module_name == "commands" else bulk_import
    monkeypatch.setattr(
        module,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not open")),
    )

    class Message:
        from_user = SimpleNamespace(id=991)
        chat = SimpleNamespace(type="group")

        def __init__(self) -> None:
            self.answers = []

        async def answer(self, text: str, **kwargs) -> None:
            self.answers.append((text, kwargs))

    async def scenario() -> None:
        message = Message()
        handler = getattr(module, handler_name)
        if command_args is None:
            await handler(message)
        else:
            await handler(message, SimpleNamespace(args=command_args))

        assert "личном чате" in message.answers[0][0]

    asyncio.run(scenario())


def test_secret_export_sinks_reject_group_before_decryption_or_database(monkeypatch) -> None:
    from guardbot.bot.handlers import commands, groups

    class Message:
        chat = SimpleNamespace(type="group")

        def __init__(self) -> None:
            self.answers = []

        async def answer(self, text: str, **kwargs) -> None:
            self.answers.append((text, kwargs))

    monkeypatch.setattr(
        commands,
        "build_mafile_dict",
        lambda _mafile: pytest.fail("secrets must not be decrypted"),
    )
    monkeypatch.setattr(
        groups,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not open")),
    )

    feedback = []

    async def record_feedback(*args, **kwargs):
        feedback.append((args, kwargs))

    monkeypatch.setattr(groups, "callback_feedback", record_feedback)

    async def scenario() -> None:
        message = Message()
        await commands.send_mafile(message, SimpleNamespace())
        assert "личном чате" in message.answers[0][0]

        callback = SimpleNamespace(
            data="export_group_1",
            from_user=SimpleNamespace(id=991),
            message=message,
            _guardbot_acknowledged=True,
        )
        await groups.callback_export_group(callback)
        assert "личном чате" in feedback[0][0][1]

    asyncio.run(scenario())


def test_perform_login_full_close_resists_repeated_cancellation(monkeypatch) -> None:
    from guardbot.bot.handlers import accounts

    class Client:
        def __init__(self) -> None:
            self.close_started = asyncio.Event()
            self.allow_close = asyncio.Event()
            self.closed = False

        async def login(self):
            return object()

        async def close(self) -> None:
            self.close_started.set()
            await self.allow_close.wait()
            self.closed = True

    class Message:
        from_user = SimpleNamespace(id=992)

        async def answer(self, *args, **kwargs):
            return SimpleNamespace(edit_text=lambda *a, **k: _async_result(None))

    client = Client()
    monkeypatch.setattr(
        accounts.SteamSessionManager,
        "create_steam_client_from_db",
        lambda *args, **kwargs: _async_result(client),
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            accounts.perform_login_full(
                Message(),
                SimpleNamespace(id=1, account_name="account"),
                SimpleNamespace(),
            )
        )
        await client.close_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        client.allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.closed is True

    asyncio.run(scenario())
