import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.handler_source import handler_function


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


def test_removed_pending_mafile_password_flow_cannot_store_payload() -> None:
    imports_source = Path("guardbot/bot/handlers/imports.py").read_text(
        encoding="utf-8"
    )
    messages_source = Path("guardbot/bot/handlers/messages.py").read_text(
        encoding="utf-8"
    )

    assert '"mafile_data": mafile_data' not in imports_source
    assert 'state_data.get("mafile_data")' not in messages_source
    assert '"encrypted_mafile_data"' not in imports_source
    assert 'state_data.get("encrypted_mafile_data")' not in messages_source


def test_device_id_is_not_echoed_back_into_telegram_history() -> None:
    source = Path("guardbot/bot/handlers/messages.py").read_text(encoding="utf-8")
    assert "Device ID: <code>{device_id}</code>" not in source


def test_secret_callbacks_use_central_feedback_instead_of_direct_ack() -> None:
    function_names = {
        "callback_lock_bot",
        "callback_cancel_password_to_settings",
        "callback_cancel_unlock",
        "callback_cancel_state_to_settings",
        "callback_remove_account_proxy",
        "callback_cancel_state_to_start",
    }

    for function_name in sorted(function_names):
        function = handler_function(function_name)
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
    function_names = {"callback_test_account_proxy", "callback_confirm_delete"}
    for function_name in sorted(function_names):
        answer_calls = [
            node
            for node in ast.walk(handler_function(function_name))
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


async def _async_result(value):
    return value


def test_manual_code_login_double_failure_still_closes_client(monkeypatch) -> None:
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageText

    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import steam_challenge

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
        chat = SimpleNamespace(type="private")

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

    class State:
        async def clear(self) -> None:
            return None

    monkeypatch.setattr(steam_challenge, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        steam_challenge,
        "get_owned_mafile",
        lambda *args, **kwargs: _async_result(mafile),
    )
    monkeypatch.setattr(
        steam_challenge.SteamSessionManager,
        "create_steam_client_from_db",
        lambda *args, **kwargs: _async_result(client),
    )

    async def scenario() -> None:
        callback = Callback()
        try:
            await steam_challenge.callback_manual_code(callback, State())
        except TelegramBadRequest:
            pass
        assert callback.answer_calls == 1
        assert client.close_calls == 1
        assert pending_challenges.current_pending_challenge_token(987) is None

    asyncio.run(scenario())


def test_action_login_acknowledges_before_missing_password_prompt(monkeypatch) -> None:
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageText

    from guardbot.bot.handlers import account_login

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
    monkeypatch.setattr(account_login, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        account_login,
        "check_proxy_available",
        lambda *args, **kwargs: _async_result((True, "proxy")),
    )
    monkeypatch.setattr(
        account_login,
        "get_owned_mafile",
        lambda *args, **kwargs: _async_result(mafile),
    )

    async def scenario() -> None:
        callback = Callback()
        try:
            await account_login.callback_action_login(callback, SimpleNamespace())
        except TelegramBadRequest:
            pass
        assert callback.answer_calls == 1

    asyncio.run(scenario())


def test_login_command_is_rejected_before_proxy_lookup_outside_private_chat(
    monkeypatch,
) -> None:
    from guardbot.bot import runtime
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

    # /login is wrapped by runtime.require_proxy, which performs the proxy
    # lookup; the command body then opens the database and starts the login.
    monkeypatch.setattr(runtime, "check_proxy_available", forbidden_proxy_lookup)
    monkeypatch.setattr(accounts, "AsyncSessionLocal", forbidden_proxy_lookup)
    monkeypatch.setattr(accounts, "perform_login_full", forbidden_proxy_lookup)

    async def scenario() -> None:
        message = Message()
        await accounts.cmd_login(message, SimpleNamespace(args="account"))
        assert message.deleted is True
        assert "личном чате" in message.answers[0]

    asyncio.run(scenario())


def test_password_login_handoff_preserves_new_challenge_state(monkeypatch) -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import account_login
    from guardbot.bot.states import LoginPasswordFlow, SteamChallengeFlow

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
            self.raw_state = LoginPasswordFlow.waiting_password.state
            self.data = {"mafile_id": 1}

        async def set_state(self, value) -> None:
            self.raw_state = getattr(value, "state", value)

        async def set_data(self, value) -> None:
            self.data = dict(value)

        async def get_state(self):
            return self.raw_state

        async def get_data(self):
            return dict(self.data)

        async def clear(self) -> None:
            self.raw_state = None
            self.data = {}

    async def perform_login(_message, _mafile, _session, *, telegram_id, state) -> None:
        # The real login publishes an email challenge after the password flow
        # was cleared; nothing in the password handler may undo it afterwards.
        assert await state.get_state() is None
        await pending_challenges.publish_pending_challenge(
            user_id=telegram_id,
            mafile_id=1,
            kind="email",
            client=client,
            state=state,
            fsm_state=SteamChallengeFlow.waiting_code,
        )

    monkeypatch.setattr(account_login, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        account_login,
        "get_owned_mafile",
        lambda *args, **kwargs: _async_result(mafile),
    )
    monkeypatch.setattr(account_login, "encrypt_value", lambda value: "encrypted")
    monkeypatch.setattr(account_login, "perform_login_full", perform_login)

    async def scenario() -> None:
        state = State()
        try:
            await account_login.receive_login_password(Message(), state)
            assert await state.get_state() == SteamChallengeFlow.waiting_code.state
            assert (await state.get_data())["kind"] == "email"
            assert pending_challenges.current_pending_challenge_token(987) is not None
            assert client.closed is False
        finally:
            await pending_challenges.clear_pending_challenges_for_user(987, state)

    asyncio.run(scenario())


def test_login_completion_edit_failure_closes_untransferred_client(monkeypatch) -> None:
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageText

    from guardbot.bot.handlers import account_login

    class Client:
        steamid = 123

        def __init__(self) -> None:
            self.close_calls = 0

        async def login(self):
            return account_login.LoginConfirmType.none

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
        account_login.SteamSessionManager,
        "create_steam_client_from_db",
        lambda *args, **kwargs: _async_result(client),
    )
    monkeypatch.setattr(
        account_login.SteamSessionManager,
        "save_steam_session_to_db",
        lambda *args, **kwargs: _async_result(None),
    )

    async def scenario() -> None:
        try:
            await account_login.perform_login_full(
                Message(),
                mafile,
                Session(),
                telegram_id=987,
                state=SimpleNamespace(),
            )
        except TelegramBadRequest:
            pass
        assert client.close_calls == 1

    asyncio.run(scenario())


def test_non_telegram_challenge_prompt_failure_rolls_back_client_state(monkeypatch) -> None:
    from guardbot.bot.handlers import account_login

    class Client:
        def __init__(self) -> None:
            self.close_calls = 0

        async def login(self):
            return account_login.LoginConfirmType.email

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
    mafile = SimpleNamespace(id=1, account_name="account", shared_secret=None)
    monkeypatch.setattr(
        account_login.SteamSessionManager,
        "create_steam_client_from_db",
        lambda *args, **kwargs: _async_result(client),
    )

    class State:
        def __init__(self) -> None:
            self.raw_state = None
            self.data = {}

        async def set_state(self, value) -> None:
            self.raw_state = getattr(value, "state", value)

        async def set_data(self, value) -> None:
            self.data = dict(value)

        async def get_state(self):
            return self.raw_state

        async def get_data(self):
            return dict(self.data)

        async def clear(self) -> None:
            self.raw_state = None
            self.data = {}

    async def scenario() -> None:
        from guardbot.bot import pending_challenges

        state = State()
        await account_login.perform_login_full(
            Message(),
            mafile,
            SimpleNamespace(),
            telegram_id=988,
            state=state,
        )
        assert pending_challenges.current_pending_challenge_token(988) is None
        assert await state.get_state() is None
        assert client.close_calls == 1

    asyncio.run(scenario())


def test_manual_code_cancellation_closes_untransferred_client(monkeypatch) -> None:
    from guardbot.bot.handlers import steam_challenge

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
        message = SimpleNamespace(
            chat=SimpleNamespace(type="private"),
            edit_text=lambda *args, **kwargs: _async_result(None),
        )

        async def answer(self, *args, **kwargs) -> None:
            return None

    class State:
        async def clear(self) -> None:
            return None

    client = Client()
    mafile = SimpleNamespace(id=1, account_name="account")
    monkeypatch.setattr(steam_challenge, "AsyncSessionLocal", Session)
    monkeypatch.setattr(
        steam_challenge,
        "get_owned_mafile",
        lambda *args, **kwargs: _async_result(mafile),
    )
    monkeypatch.setattr(
        steam_challenge.SteamSessionManager,
        "create_steam_client_from_db",
        lambda *args, **kwargs: _async_result(client),
    )

    async def scenario() -> None:
        task = asyncio.create_task(steam_challenge.callback_manual_code(Callback(), State()))
        await client.login_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.closed is True

    asyncio.run(scenario())


def test_manual_code_cancellation_after_state_commit_clears_transferred_client(monkeypatch) -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import steam_challenge
    from guardbot.steam.client import LoginConfirmType
    from guardbot.bot.states import SteamChallengeFlow

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Client:
        def __init__(self) -> None:
            self.closed = False

        async def login(self):
            return LoginConfirmType.mobile

        async def close(self) -> None:
            self.closed = True

    edit_started = asyncio.Event()

    class Message:
        chat = SimpleNamespace(type="private")

        async def edit_text(self, *args, **kwargs) -> None:
            edit_started.set()
            await asyncio.Event().wait()

    class Callback:
        data = "manual_code_1"
        from_user = SimpleNamespace(id=990)
        message = Message()

        async def answer(self, *args, **kwargs) -> None:
            return None

    class State:
        def __init__(self) -> None:
            self.raw_state = None
            self.data = {}

        async def set_state(self, value) -> None:
            self.raw_state = getattr(value, "state", value)

        async def set_data(self, value) -> None:
            self.data = dict(value)

        async def get_state(self):
            return self.raw_state

        async def get_data(self):
            return dict(self.data)

        async def clear(self) -> None:
            self.raw_state = None
            self.data = {}

    client = Client()
    mafile = SimpleNamespace(id=1, account_name="account")
    monkeypatch.setattr(steam_challenge, "AsyncSessionLocal", Session)
    monkeypatch.setattr(
        steam_challenge,
        "get_owned_mafile",
        lambda *args, **kwargs: _async_result(mafile),
    )
    monkeypatch.setattr(
        steam_challenge.SteamSessionManager,
        "create_steam_client_from_db",
        lambda *args, **kwargs: _async_result(client),
    )

    async def scenario() -> None:
        state = State()
        task = asyncio.create_task(steam_challenge.callback_manual_code(Callback(), state))
        await edit_started.wait()
        assert await state.get_state() == SteamChallengeFlow.waiting_code.state
        assert pending_challenges.pending_challenge_count() == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert await state.get_state() is None
        assert await state.get_data() == {}
        assert client.closed is True
        assert pending_challenges.pending_challenge_count() == 0

    asyncio.run(scenario())


def test_manual_code_cancellation_preserves_newer_replacement_state(monkeypatch) -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import steam_challenge
    from guardbot.steam.client import LoginConfirmType
    from guardbot.bot.states import SteamChallengeFlow

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Client:
        def __init__(self) -> None:
            self.closed = False

        async def login(self):
            return LoginConfirmType.mobile

        async def close(self) -> None:
            self.closed = True

    edit_started = asyncio.Event()

    class Message:
        chat = SimpleNamespace(type="private")

        async def edit_text(self, *args, **kwargs) -> None:
            edit_started.set()
            await asyncio.Event().wait()

    class Callback:
        data = "manual_code_1"
        from_user = SimpleNamespace(id=994)
        message = Message()

        async def answer(self, *args, **kwargs) -> None:
            return None

    class State:
        def __init__(self) -> None:
            self.raw_state = None
            self.data = {}

        async def set_state(self, value) -> None:
            self.raw_state = getattr(value, "state", value)

        async def set_data(self, value) -> None:
            self.data = dict(value)

        async def get_state(self):
            return self.raw_state

        async def get_data(self):
            return dict(self.data)

        async def clear(self) -> None:
            self.raw_state = None
            self.data = {}

    challenge_client = Client()
    replacement_client = Client()
    monkeypatch.setattr(steam_challenge, "AsyncSessionLocal", Session)
    monkeypatch.setattr(
        steam_challenge,
        "get_owned_mafile",
        lambda *args, **kwargs: _async_result(
            SimpleNamespace(id=1, account_name="account")
        ),
    )
    monkeypatch.setattr(
        steam_challenge.SteamSessionManager,
        "create_steam_client_from_db",
        lambda *args, **kwargs: _async_result(challenge_client),
    )

    async def scenario() -> None:
        state = State()
        task = asyncio.create_task(steam_challenge.callback_manual_code(Callback(), state))
        await edit_started.wait()
        original_token = (await state.get_data())["challenge_id"]
        replacement_token = await pending_challenges.publish_pending_challenge(
            user_id=994,
            mafile_id=1,
            kind="manual_mobile",
            client=replacement_client,
            state=state,
            fsm_state=SteamChallengeFlow.waiting_code,
            ttl_seconds=60,
        )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        try:
            assert original_token != replacement_token
            assert await state.get_state() == SteamChallengeFlow.waiting_code.state
            assert (await state.get_data())["challenge_id"] == replacement_token
            assert challenge_client.closed is True
            assert replacement_client.closed is False
        finally:
            await pending_challenges.clear_pending_challenges_for_user(994)

    asyncio.run(scenario())


def test_successful_single_mafile_import_clears_terminal_state(monkeypatch) -> None:
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
        chat = SimpleNamespace(type="private")
        from_user = SimpleNamespace(id=991)
        document = SimpleNamespace(
            file_name="account.mafile",
            file_size=100,
            file_id="file-id",
        )

        async def delete(self) -> None:
            return None

        async def answer(self, *args, **kwargs):
            return StatusMessage()

    class State:
        raw_state = "NewMaFileImportFlow:waiting_document"

        async def get_state(self):
            return self.raw_state

        async def clear(self) -> None:
            self.raw_state = None

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
        state = State()
        await imports.receive_new_mafile(Message(), state)
        assert state.raw_state is None
        confirmation = status_edits[-1][0]
        assert 'x&lt;/b&gt;&lt;a href=&quot;https://example.invalid/&quot;&gt;' in confirmation
        assert '<a href="https://example.invalid/">' not in confirmation

    asyncio.run(scenario())


def test_confirm_delete_escapes_account_name_after_commit(monkeypatch) -> None:
    from guardbot.bot.handlers import account_maintenance

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

    monkeypatch.setattr(account_maintenance, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(account_maintenance, "get_owned_mafile", owned)

    async def scenario() -> None:
        await account_maintenance.callback_confirm_delete(callback)

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
    from guardbot.bot.handlers import account_login

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
        account_login.SteamSessionManager,
        "create_steam_client_from_db",
        lambda *args, **kwargs: _async_result(client),
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            account_login.perform_login_full(
                Message(),
                SimpleNamespace(id=1, account_name="account"),
                SimpleNamespace(),
                telegram_id=992,
                state=SimpleNamespace(),
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
