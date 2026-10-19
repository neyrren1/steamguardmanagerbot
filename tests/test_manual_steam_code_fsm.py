import asyncio

import pytest


class Client:
    def __init__(self) -> None:
        self.closed = False
        self.close_calls = 0
        self.shared_secret = "shared"
        self.steamid = 76561198000000000

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True

    async def login(self) -> object:
        return None

    async def confirm_login(self, code: str) -> None:
        return None


class FakeState:
    def __init__(self) -> None:
        self.raw_state = None
        self.data = {}

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


def test_manual_steam_code_uses_typed_state() -> None:
    from guardbot.bot.states import SteamChallengeFlow

    assert SteamChallengeFlow.waiting_code.state == "SteamChallengeFlow:waiting_code"


def test_pending_challenge_registry_leases_and_expires_client() -> None:
    from guardbot.bot import pending_challenges

    async def scenario() -> None:
        client = Client()
        token = await pending_challenges._store_pending_challenge(
            user_id=501,
            mafile_id=42,
            kind="manual_mobile",
            client=client,
            ttl_seconds=0.01,
        )
        assert token.isascii()
        assert len(token) == 32

        entry = await pending_challenges.lease_pending_challenge(
            token,
            user_id=501,
            mafile_id=42,
            kind="manual_mobile",
        )
        assert entry is not None
        assert entry.client is client

        await asyncio.sleep(0.03)
        assert client.closed is False

        await pending_challenges.release_pending_challenge(token, entry)
        await asyncio.sleep(0.03)
        assert client.closed is True
        assert pending_challenges.pending_challenge_count() == 0

    asyncio.run(scenario())


def test_pending_challenge_registry_rejects_wrong_owner_and_clears_client() -> None:
    from guardbot.bot import pending_challenges

    async def scenario() -> None:
        client = Client()
        token = await pending_challenges._store_pending_challenge(
            user_id=502,
            mafile_id=43,
            kind="manual_mobile",
            client=client,
            ttl_seconds=60,
        )
        try:
            assert await pending_challenges.lease_pending_challenge(
                token,
                user_id=999,
                mafile_id=43,
                kind="manual_mobile",
            ) is None
            assert client.closed is False
        finally:
            await pending_challenges.clear_pending_challenges_for_user(502)

        assert client.closed is True
        assert client.close_calls == 1
        assert pending_challenges.pending_challenge_count() == 0

    asyncio.run(scenario())


def test_pending_challenge_clear_finishes_before_cancellation_escapes() -> None:
    from guardbot.bot import pending_challenges

    class BlockingClient(Client):
        def __init__(self) -> None:
            super().__init__()
            self.close_started = asyncio.Event()
            self.allow_close = asyncio.Event()

        async def close(self) -> None:
            self.close_calls += 1
            self.close_started.set()
            await self.allow_close.wait()
            self.closed = True

    async def scenario() -> None:
        client = BlockingClient()
        await pending_challenges._store_pending_challenge(
            user_id=503,
            mafile_id=44,
            kind="manual_mobile",
            client=client,
            ttl_seconds=60,
        )
        task = asyncio.create_task(
            pending_challenges.clear_pending_challenges_for_user(503)
        )
        await client.close_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        client.allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert client.closed is True
        assert client.close_calls == 1
        assert pending_challenges.pending_challenge_count() == 0

    asyncio.run(scenario())


def test_clear_user_workflows_closes_pending_challenge() -> None:
    from guardbot.bot import pending_challenges, runtime

    class State:
        def __init__(self) -> None:
            self.cleared = False

        async def clear(self) -> None:
            self.cleared = True

    async def scenario() -> None:
        client = Client()
        state = State()
        await pending_challenges._store_pending_challenge(
            user_id=504,
            mafile_id=45,
            kind="manual_mobile",
            client=client,
            ttl_seconds=60,
        )

        await runtime.clear_user_workflows(504, state)

        assert state.cleared is True
        assert client.closed is True
        assert pending_challenges.pending_challenge_count() == 0

    asyncio.run(scenario())


def test_manual_code_callback_arms_typed_state_before_prompt(monkeypatch) -> None:
    from types import SimpleNamespace

    from guardbot.bot import pending_challenges, runtime
    from guardbot.bot.handlers import steam_challenge
    from guardbot.steam.client import LoginConfirmType
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 505
    state = FakeState()
    client = Client()
    mafile = SimpleNamespace(id=42, account_name="account")

    async def login():
        return LoginConfirmType.mobile

    client.login = login

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Message:
        chat = SimpleNamespace(type="private")

        def __init__(self) -> None:
            self.snapshots = []

        async def edit_text(self, *_args, **_kwargs) -> None:
            self.snapshots.append((await state.get_state(), await state.get_data()))

    class Callback:
        data = "manual_code_42"
        from_user = SimpleNamespace(id=user_id)

        def __init__(self) -> None:
            self.message = Message()

    async def owned(*_args, **_kwargs):
        return mafile

    async def client_from_db(*_args, **_kwargs):
        return client

    async def feedback(*_args, **_kwargs):
        return None

    monkeypatch.setattr(steam_challenge, "AsyncSessionLocal", Session)
    monkeypatch.setattr(steam_challenge, "get_owned_mafile", owned)
    monkeypatch.setattr(
        steam_challenge.SteamSessionManager,
        "create_steam_client_from_db",
        client_from_db,
    )
    monkeypatch.setattr(steam_challenge, "callback_feedback", feedback)

    async def scenario() -> None:
        callback = Callback()
        try:
            await steam_challenge.callback_manual_code(callback, state)

            assert await state.get_state() == SteamChallengeFlow.waiting_code.state
            data = await state.get_data()
            assert set(data) == {"challenge_id", "mafile_id", "kind"}
            assert data["mafile_id"] == 42
            assert data["kind"] == "manual_mobile"
            assert isinstance(data["challenge_id"], str)
            assert len(data["challenge_id"]) == 32
            assert callback.message.snapshots == [
                (SteamChallengeFlow.waiting_code.state, data)
            ]
            assert pending_challenges.pending_challenge_count() == 1
            assert client.closed is False
        finally:
            await runtime.clear_user_workflows(user_id, state)

    asyncio.run(scenario())


def test_manual_code_success_saves_session_and_closes_registry_client(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import steam_challenge
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 506
    mafile = SimpleNamespace(id=42, account_name="account")
    client = Client()
    confirmed_codes = []
    saved = []
    save_options = []

    async def confirm_login(code: str) -> None:
        confirmed_codes.append(code)

    client.confirm_login = confirm_login

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Status:
        def __init__(self) -> None:
            self.edits = []

        async def edit_text(self, text: str, **kwargs) -> None:
            self.edits.append((text, kwargs))

    class Message:
        from_user = SimpleNamespace(id=user_id)
        chat = SimpleNamespace(type="private")
        text = "ABCDE"

        def __init__(self) -> None:
            self.deleted = 0
            self.answers = []
            self.status = Status()

        async def delete(self) -> None:
            self.deleted += 1

        async def answer(self, text: str, **kwargs):
            self.answers.append((text, kwargs))
            return self.status

    async def owned(*_args, **_kwargs):
        return mafile

    async def save(*args, **kwargs) -> bool:
        saved.append(args)
        save_options.append(kwargs)
        return True

    monkeypatch.setattr(steam_challenge, "AsyncSessionLocal", Session)
    monkeypatch.setattr(steam_challenge, "get_owned_mafile", owned)
    monkeypatch.setattr(steam_challenge.SteamSessionManager, "save_steam_session_to_db", save)

    async def scenario() -> None:
        state = FakeState()
        token = await pending_challenges._store_pending_challenge(
            user_id=user_id,
            mafile_id=42,
            kind="manual_mobile",
            client=client,
            ttl_seconds=60,
        )
        await state.set_state(SteamChallengeFlow.waiting_code)
        await state.set_data(
            {"challenge_id": token, "mafile_id": 42, "kind": "manual_mobile"}
        )
        message = Message()

        await steam_challenge.receive_manual_steam_code(message, state)

        assert message.deleted == 1
        assert confirmed_codes == ["ABCDE"]
        assert len(saved) == 1
        assert save_options == [{"telegram_id": user_id}]
        assert await state.get_state() is None
        assert await state.get_data() == {}
        assert pending_challenges.pending_challenge_count() == 0
        assert client.closed is True
        assert message.status.edits

    asyncio.run(scenario())


def test_invalid_manual_code_keeps_typed_state_and_live_client_retryable(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import steam_challenge
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 507
    client = Client()

    async def confirm_login(code: str) -> None:
        raise steam_challenge.InvalidSteamGuardCode()

    client.confirm_login = confirm_login

    class Message:
        from_user = SimpleNamespace(id=user_id)
        chat = SimpleNamespace(type="private")
        text = "WRONG"

        def __init__(self) -> None:
            self.deleted = 0
            self.answers = []
            self.status_edits = []

        async def delete(self) -> None:
            self.deleted += 1

        async def answer(self, text: str, **kwargs):
            self.answers.append((text, kwargs))

            async def edit_text(value: str, **edit_kwargs) -> None:
                self.status_edits.append((value, edit_kwargs))

            return SimpleNamespace(edit_text=edit_text)

    async def scenario() -> None:
        state = FakeState()
        token = await pending_challenges._store_pending_challenge(
            user_id=user_id,
            mafile_id=42,
            kind="manual_mobile",
            client=client,
            ttl_seconds=60,
        )
        data = {"challenge_id": token, "mafile_id": 42, "kind": "manual_mobile"}
        await state.set_state(SteamChallengeFlow.waiting_code)
        await state.set_data(data)
        message = Message()

        try:
            await steam_challenge.receive_manual_steam_code(message, state)

            assert message.deleted == 1
            assert await state.get_state() == SteamChallengeFlow.waiting_code.state
            assert await state.get_data() == data
            assert pending_challenges.pending_challenge_count() == 1
            assert client.closed is False
            assert message.answers == [("🔄 Проверяю код...", {})]
            assert message.status_edits == [
                ("❌ Неверный код подтверждения. Попробуйте ещё раз.", {})
            ]
        finally:
            await pending_challenges.clear_pending_challenges_for_user(user_id)

    asyncio.run(scenario())


def test_manual_code_prompt_failure_rolls_back_state_and_client(monkeypatch) -> None:
    from types import SimpleNamespace

    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageText

    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import steam_challenge
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 508
    state = FakeState()
    client = Client()
    mafile = SimpleNamespace(id=42, account_name="account")

    async def login() -> None:
        return None

    client.login = login

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Prompt:
        chat = SimpleNamespace(type="private")

        def __init__(self) -> None:
            self.state_at_edit = None

        async def edit_text(self, *_args, **_kwargs) -> None:
            self.state_at_edit = await state.get_state()
            raise TelegramBadRequest(
                method=EditMessageText(text="prompt", chat_id=user_id, message_id=1),
                message="edit failed",
            )

    class Callback:
        data = "manual_code_42"
        from_user = SimpleNamespace(id=user_id)

        def __init__(self) -> None:
            self.message = Prompt()

        async def answer(self, *_args, **_kwargs) -> None:
            return None

    async def owned(*_args, **_kwargs):
        return mafile

    async def create(*_args, **_kwargs):
        return client

    async def feedback(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(steam_challenge, "AsyncSessionLocal", Session)
    monkeypatch.setattr(steam_challenge, "get_owned_mafile", owned)
    monkeypatch.setattr(
        steam_challenge.SteamSessionManager,
        "create_steam_client_from_db",
        create,
    )
    monkeypatch.setattr(steam_challenge, "callback_feedback", feedback)

    async def scenario() -> None:
        callback = Callback()
        with pytest.raises(TelegramBadRequest):
            await steam_challenge.callback_manual_code(callback, state)

        assert callback.message.state_at_edit == SteamChallengeFlow.waiting_code.state
        assert await state.get_state() is None
        assert await state.get_data() == {}
        assert client.closed is True
        assert pending_challenges.pending_challenge_count() == 0

    asyncio.run(scenario())


def test_manual_code_delete_failure_does_not_read_state_or_claim_client() -> None:
    from types import SimpleNamespace

    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import DeleteMessage

    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import steam_challenge

    user_id = 509
    client = Client()

    class State(FakeState):
        async def get_data(self):
            raise AssertionError("FSM data must not be read after deletion failure")

    class Message:
        from_user = SimpleNamespace(id=user_id)
        chat = SimpleNamespace(type="private")
        text = "ABCDE"

        def __init__(self) -> None:
            self.answers = []

        async def delete(self) -> None:
            raise TelegramBadRequest(
                method=DeleteMessage(chat_id=user_id, message_id=1),
                message="delete failed",
            )

        async def answer(self, text: str, **kwargs) -> None:
            self.answers.append((text, kwargs))

    async def scenario() -> None:
        token = await pending_challenges._store_pending_challenge(
            user_id=user_id,
            mafile_id=42,
            kind="manual_mobile",
            client=client,
            ttl_seconds=60,
        )
        try:
            await steam_challenge.receive_manual_steam_code(Message(), State())
            assert pending_challenges.pending_challenge_count() == 1
            assert client.closed is False
        finally:
            await pending_challenges.remove_pending_challenge(token)

    asyncio.run(scenario())


def test_registered_manual_code_handler_rejects_commands_and_precedes_catch_all() -> None:
    from datetime import UTC, datetime

    from aiogram.types import Chat, Message, User

    from guardbot.bot.handlers import steam_challenge, messages
    from guardbot.bot.states import SteamChallengeFlow

    command = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=510, type="private"),
        from_user=User(id=510, is_bot=False, first_name="Test"),
        text="/accounts",
    )
    code = Message(
        message_id=2,
        date=datetime.now(UTC),
        chat=Chat(id=510, type="private"),
        from_user=User(id=510, is_bot=False, first_name="Test"),
        text="ABCDE",
    )
    from guardbot.bot import routing, runtime

    handlers = routing.ordered_handlers(runtime.dp, "message")
    typed_handler = next(
        handler
        for handler in handlers
        if handler.callback is steam_challenge.receive_manual_steam_code
    )
    typed_index = handlers.index(typed_handler)
    catch_all_index = next(
        index
        for index, handler in enumerate(handlers)
        if handler.callback is messages.handle_messages
    )

    async def scenario() -> None:
        command_match, _ = await typed_handler.check(
            command,
            raw_state=SteamChallengeFlow.waiting_code.state,
        )
        code_match, _ = await typed_handler.check(
            code,
            raw_state=SteamChallengeFlow.waiting_code.state,
        )
        assert command_match is False
        assert code_match is True
        assert typed_index < catch_all_index

    asyncio.run(scenario())


def test_manual_code_start_reports_required_proxy_without_publishing_client(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import steam_challenge

    user_id = 511
    state = FakeState()
    mafile = SimpleNamespace(id=42, account_name="account")

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class Prompt:
        chat = SimpleNamespace(type="private")

        def __init__(self) -> None:
            self.edits = []

        async def edit_text(self, text: str, **kwargs) -> None:
            self.edits.append((text, kwargs))

    class Callback:
        data = "manual_code_42"
        from_user = SimpleNamespace(id=user_id)

        def __init__(self) -> None:
            self.message = Prompt()

        async def answer(self, *_args, **_kwargs) -> None:
            return None

    async def owned(*_args, **_kwargs):
        return mafile

    async def create(*_args, **_kwargs):
        raise steam_challenge.ProxyRequiredError("sensitive account detail")

    monkeypatch.setattr(steam_challenge, "AsyncSessionLocal", Session)
    monkeypatch.setattr(steam_challenge, "get_owned_mafile", owned)
    monkeypatch.setattr(
        steam_challenge.SteamSessionManager,
        "create_steam_client_from_db",
        create,
    )

    async def scenario() -> None:
        callback = Callback()
        await steam_challenge.callback_manual_code(callback, state)

        assert await state.get_state() is None
        assert pending_challenges.pending_challenge_count() == 0
        assert len(callback.message.edits) == 1
        assert "прокси" in callback.message.edits[0][0].lower()
        assert "sensitive account detail" not in callback.message.edits[0][0]

    asyncio.run(scenario())


def test_manual_code_start_cancellation_during_session_exit_closes_client(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import steam_challenge

    user_id = 512
    state = FakeState()
    client = Client()
    mafile = SimpleNamespace(id=42, account_name="account")

    class Session:
        exit_started = asyncio.Event()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            self.exit_started.set()
            await asyncio.Event().wait()

    class Prompt:
        chat = SimpleNamespace(type="private")

        async def edit_text(self, *_args, **_kwargs) -> None:
            return None

    class Callback:
        data = "manual_code_42"
        from_user = SimpleNamespace(id=user_id)
        message = Prompt()

    async def owned(*_args, **_kwargs):
        return mafile

    async def create(*_args, **_kwargs):
        return client

    async def feedback(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(steam_challenge, "AsyncSessionLocal", Session)
    monkeypatch.setattr(steam_challenge, "get_owned_mafile", owned)
    monkeypatch.setattr(
        steam_challenge.SteamSessionManager,
        "create_steam_client_from_db",
        create,
    )
    monkeypatch.setattr(steam_challenge, "callback_feedback", feedback)

    async def scenario() -> None:
        task = asyncio.create_task(steam_challenge.callback_manual_code(Callback(), state))
        await Session.exit_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert client.closed is True
        assert client.close_calls == 1
        assert pending_challenges.pending_challenge_count() == 0

    asyncio.run(scenario())


def test_manual_code_cancellation_during_fsm_read_closes_pending_client() -> None:
    from types import SimpleNamespace

    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import steam_challenge
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 513
    client = Client()

    class BlockingState(FakeState):
        def __init__(self, token: str) -> None:
            super().__init__()
            self.raw_state = SteamChallengeFlow.waiting_code.state
            self.data = {
                "challenge_id": token,
                "mafile_id": 42,
                "kind": "manual_mobile",
            }
            self.read_started = asyncio.Event()

        async def get_data(self) -> dict:
            self.read_started.set()
            await asyncio.Event().wait()

    class Message:
        from_user = SimpleNamespace(id=user_id)
        chat = SimpleNamespace(type="private")
        text = "ABCDE"

        async def delete(self) -> None:
            return None

    async def scenario() -> None:
        token = await pending_challenges._store_pending_challenge(
            user_id=user_id,
            mafile_id=42,
            kind="manual_mobile",
            client=client,
            ttl_seconds=60,
        )
        state = BlockingState(token)
        task = asyncio.create_task(steam_challenge.receive_manual_steam_code(Message(), state))
        await state.read_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert client.closed is True
        assert pending_challenges.pending_challenge_count() == 0
        assert state.raw_state is None
        assert state.data == {}

    asyncio.run(scenario())


def test_invalid_manual_code_feedback_failure_closes_pending_client() -> None:
    from types import SimpleNamespace

    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import steam_challenge
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 514
    client = Client()

    class Message:
        from_user = SimpleNamespace(id=user_id)
        chat = SimpleNamespace(type="private")
        text = "X" * 65

        async def delete(self) -> None:
            return None

        async def answer(self, *_args, **_kwargs) -> None:
            raise RuntimeError("feedback failed")

    async def scenario() -> None:
        token = await pending_challenges._store_pending_challenge(
            user_id=user_id,
            mafile_id=42,
            kind="manual_mobile",
            client=client,
            ttl_seconds=60,
        )
        state = FakeState()
        await state.set_data(
            {
                "challenge_id": token,
                "mafile_id": 42,
                "kind": "manual_mobile",
            }
        )
        await state.set_state(SteamChallengeFlow.waiting_code)

        with pytest.raises(RuntimeError, match="feedback failed"):
            await steam_challenge.receive_manual_steam_code(Message(), state)

        assert client.closed is True
        assert pending_challenges.pending_challenge_count() == 0
        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(scenario())


def test_manual_code_cancellation_while_acquiring_lease_closes_pending_client(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import steam_challenge
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 515
    client = Client()
    lease_started = asyncio.Event()

    class Message:
        from_user = SimpleNamespace(id=user_id)
        chat = SimpleNamespace(type="private")
        text = "ABCDE"

        async def delete(self) -> None:
            return None

    async def blocking_lease(*_args, **_kwargs):
        lease_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(steam_challenge, "lease_pending_challenge", blocking_lease)

    async def scenario() -> None:
        token = await pending_challenges._store_pending_challenge(
            user_id=user_id,
            mafile_id=42,
            kind="manual_mobile",
            client=client,
            ttl_seconds=60,
        )
        state = FakeState()
        await state.set_data(
            {
                "challenge_id": token,
                "mafile_id": 42,
                "kind": "manual_mobile",
            }
        )
        await state.set_state(SteamChallengeFlow.waiting_code)

        task = asyncio.create_task(steam_challenge.receive_manual_steam_code(Message(), state))
        await lease_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert client.closed is True
        assert pending_challenges.pending_challenge_count() == 0
        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(scenario())


def test_fsm_read_cancellation_preserves_concurrently_published_replacement() -> None:
    from types import SimpleNamespace

    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import steam_challenge
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 516
    old_client = Client()
    replacement_client = Client()
    read_started = asyncio.Event()
    allow_read = asyncio.Event()

    class BlockingState(FakeState):
        async def get_data(self) -> dict:
            read_started.set()
            await allow_read.wait()
            return dict(self.data)

    class Message:
        from_user = SimpleNamespace(id=user_id)
        chat = SimpleNamespace(type="private")
        text = "ABCDE"

        async def delete(self) -> None:
            return None

    async def scenario() -> None:
        state = BlockingState()
        old_token = await pending_challenges._store_pending_challenge(
            user_id=user_id,
            mafile_id=42,
            kind="manual_mobile",
            client=old_client,
            ttl_seconds=60,
        )
        await state.set_data(
            {
                "challenge_id": old_token,
                "mafile_id": 42,
                "kind": "manual_mobile",
            }
        )
        await state.set_state(SteamChallengeFlow.waiting_code)

        old_task = asyncio.create_task(
            steam_challenge.receive_manual_steam_code(Message(), state)
        )
        await read_started.wait()
        replacement_task = asyncio.create_task(
            pending_challenges.publish_pending_challenge(
                user_id=user_id,
                mafile_id=43,
                kind="manual_mobile",
                client=replacement_client,
                state=state,
                fsm_state=SteamChallengeFlow.waiting_code,
                ttl_seconds=60,
            )
        )
        await asyncio.sleep(0)
        old_task.cancel()
        allow_read.set()
        with pytest.raises(asyncio.CancelledError):
            await old_task
        replacement_token = await replacement_task

        try:
            assert old_client.closed is True
            assert replacement_client.closed is False
            assert pending_challenges.pending_challenge_count() == 1
            assert await state.get_state() == SteamChallengeFlow.waiting_code.state
            assert await state.get_data() == {
                "challenge_id": replacement_token,
                "mafile_id": 43,
                "kind": "manual_mobile",
            }
        finally:
            await pending_challenges.clear_pending_challenges_for_user(
                user_id,
                state,
            )

    asyncio.run(scenario())


def test_exact_cleanup_and_replacement_publication_are_atomic() -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 517
    old_client = Client()
    replacement_client = Client()
    clear_started = asyncio.Event()
    allow_clear = asyncio.Event()
    block_clear = False

    class BlockingState(FakeState):
        async def clear(self) -> None:
            if block_clear:
                clear_started.set()
                await allow_clear.wait()
            await super().clear()

    async def scenario() -> None:
        nonlocal block_clear
        state = BlockingState()
        old_token = await pending_challenges.publish_pending_challenge(
            user_id=user_id,
            mafile_id=42,
            kind="manual_mobile",
            client=old_client,
            state=state,
            fsm_state=SteamChallengeFlow.waiting_code,
            ttl_seconds=60,
        )
        old_entry = await pending_challenges.lease_pending_challenge(
            old_token,
            user_id=user_id,
            mafile_id=42,
            kind="manual_mobile",
        )
        assert old_entry is not None
        block_clear = True

        cleanup_task = asyncio.create_task(
            pending_challenges.clear_owned_pending_challenge(
                old_token,
                state,
                user_id=user_id,
                expected_entry=old_entry,
            )
        )
        await clear_started.wait()
        replacement_task = asyncio.create_task(
            pending_challenges.publish_pending_challenge(
                user_id=user_id,
                mafile_id=43,
                kind="manual_mobile",
                client=replacement_client,
                state=state,
                fsm_state=SteamChallengeFlow.waiting_code,
                ttl_seconds=60,
            )
        )
        await asyncio.sleep(0)
        allow_clear.set()
        await cleanup_task
        replacement_token = await replacement_task

        try:
            assert old_client.closed is True
            assert replacement_client.closed is False
            assert pending_challenges.pending_challenge_count() == 1
            assert await state.get_state() == SteamChallengeFlow.waiting_code.state
            assert await state.get_data() == {
                "challenge_id": replacement_token,
                "mafile_id": 43,
                "kind": "manual_mobile",
            }
        finally:
            await pending_challenges.clear_pending_challenges_for_user(
                user_id,
                state,
            )

    asyncio.run(scenario())


def test_invalid_code_release_failure_closes_leased_challenge(monkeypatch) -> None:
    from types import SimpleNamespace

    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import steam_challenge
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 518
    client = Client()

    async def confirm_login(code: str) -> None:
        raise steam_challenge.InvalidSteamGuardCode()

    async def failing_release(*_args, **_kwargs) -> None:
        raise RuntimeError("release failed")

    client.confirm_login = confirm_login
    monkeypatch.setattr(steam_challenge, "release_pending_challenge", failing_release)

    class Status:
        async def edit_text(self, *_args, **_kwargs) -> None:
            return None

    class Message:
        from_user = SimpleNamespace(id=user_id)
        chat = SimpleNamespace(type="private")
        text = "WRONG"

        async def delete(self) -> None:
            return None

        async def answer(self, *_args, **_kwargs):
            return Status()

    async def scenario() -> None:
        state = FakeState()
        token = await pending_challenges.publish_pending_challenge(
            user_id=user_id,
            mafile_id=42,
            kind="manual_mobile",
            client=client,
            state=state,
            fsm_state=SteamChallengeFlow.waiting_code,
            ttl_seconds=60,
        )

        with pytest.raises(RuntimeError, match="release failed"):
            await steam_challenge.receive_manual_steam_code(Message(), state)

        assert token
        assert client.closed is True
        assert pending_challenges.pending_challenge_count() == 0
        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(scenario())


def test_publish_cancellation_while_retiring_old_client_rolls_back_safely() -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 519
    close_started = asyncio.Event()
    allow_close = asyncio.Event()

    class BlockingCloseClient(Client):
        async def close(self) -> None:
            self.close_calls += 1
            close_started.set()
            await allow_close.wait()
            self.closed = True

    old_client = BlockingCloseClient()
    replacement_client = Client()

    async def scenario() -> None:
        state = FakeState()
        await pending_challenges.publish_pending_challenge(
            user_id=user_id,
            mafile_id=42,
            kind="manual_mobile",
            client=old_client,
            state=state,
            fsm_state=SteamChallengeFlow.waiting_code,
            ttl_seconds=60,
        )

        replacement_task = asyncio.create_task(
            pending_challenges.publish_pending_challenge(
                user_id=user_id,
                mafile_id=43,
                kind="manual_mobile",
                client=replacement_client,
                state=state,
                fsm_state=SteamChallengeFlow.waiting_code,
                ttl_seconds=60,
            )
        )
        await close_started.wait()
        replacement_task.cancel()
        allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await replacement_task

        assert old_client.closed is True
        assert old_client.close_calls == 1
        assert replacement_client.closed is True
        assert replacement_client.close_calls == 1
        assert pending_challenges.pending_challenge_count() == 0
        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(scenario())


def test_publish_cancellation_during_state_snapshot_preserves_old_challenge() -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 520
    old_client = Client()
    replacement_client = Client()
    snapshot_started = asyncio.Event()
    allow_snapshot = asyncio.Event()

    class BlockingState(FakeState):
        async def get_data(self) -> dict:
            snapshot_started.set()
            await allow_snapshot.wait()
            return dict(self.data)

    async def scenario() -> None:
        state = BlockingState()
        old_token = await pending_challenges._store_pending_challenge(
            user_id=user_id,
            mafile_id=42,
            kind="manual_mobile",
            client=old_client,
            ttl_seconds=60,
        )
        await state.set_data(
            {
                "challenge_id": old_token,
                "mafile_id": 42,
                "kind": "manual_mobile",
            }
        )
        await state.set_state(SteamChallengeFlow.waiting_code)

        replacement_task = asyncio.create_task(
            pending_challenges.publish_pending_challenge(
                user_id=user_id,
                mafile_id=43,
                kind="manual_mobile",
                client=replacement_client,
                state=state,
                fsm_state=SteamChallengeFlow.waiting_code,
                ttl_seconds=60,
            )
        )
        await snapshot_started.wait()
        replacement_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await replacement_task

        try:
            assert old_client.closed is False
            assert replacement_client.closed is True
            assert replacement_client.close_calls == 1
            assert pending_challenges.pending_challenge_count() == 1
            assert await state.get_state() == SteamChallengeFlow.waiting_code.state
            allow_snapshot.set()
            assert await state.get_data() == {
                "challenge_id": old_token,
                "mafile_id": 42,
                "kind": "manual_mobile",
            }
        finally:
            allow_snapshot.set()
            await pending_challenges.clear_pending_challenges_for_user(
                user_id,
                state,
            )

    asyncio.run(scenario())


def test_publish_rejects_replacement_while_old_challenge_is_leased() -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 521
    old_client = Client()
    replacement_client = Client()

    async def scenario() -> None:
        state = FakeState()
        old_token = await pending_challenges.publish_pending_challenge(
            user_id=user_id,
            mafile_id=42,
            kind="manual_mobile",
            client=old_client,
            state=state,
            fsm_state=SteamChallengeFlow.waiting_code,
            ttl_seconds=60,
        )
        old_entry = await pending_challenges.lease_pending_challenge(
            old_token,
            user_id=user_id,
            mafile_id=42,
            kind="manual_mobile",
        )
        assert old_entry is not None

        with pytest.raises(RuntimeError, match="leased pending challenge"):
            await pending_challenges.publish_pending_challenge(
                user_id=user_id,
                mafile_id=43,
                kind="manual_mobile",
                client=replacement_client,
                state=state,
                fsm_state=SteamChallengeFlow.waiting_code,
                ttl_seconds=60,
            )

        try:
            assert old_client.closed is False
            assert replacement_client.closed is True
            assert replacement_client.close_calls == 1
            assert pending_challenges.pending_challenge_count() == 1
            assert await state.get_state() == SteamChallengeFlow.waiting_code.state
            assert await state.get_data() == {
                "challenge_id": old_token,
                "mafile_id": 42,
                "kind": "manual_mobile",
            }
        finally:
            await pending_challenges.clear_pending_challenges_for_user(
                user_id,
                state,
            )

    asyncio.run(scenario())


def test_publish_cancellation_while_waiting_for_user_lock_closes_incoming_client() -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 522
    incoming_client = Client()

    async def scenario() -> None:
        state = FakeState()
        lock = pending_challenges._user_lock(user_id)
        await lock.acquire()
        task = asyncio.create_task(
            pending_challenges.publish_pending_challenge(
                user_id=user_id,
                mafile_id=42,
                kind="manual_mobile",
                client=incoming_client,
                state=state,
                fsm_state=SteamChallengeFlow.waiting_code,
                ttl_seconds=60,
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            lock.release()

        assert incoming_client.closed is True
        assert incoming_client.close_calls == 1
        assert pending_challenges.pending_challenge_count() == 0
        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(scenario())


def test_read_cancellation_while_waiting_for_user_lock_cleans_exact_challenge() -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 523
    client = Client()

    async def scenario() -> None:
        state = FakeState()
        token = await pending_challenges.publish_pending_challenge(
            user_id=user_id,
            mafile_id=42,
            kind="manual_mobile",
            client=client,
            state=state,
            fsm_state=SteamChallengeFlow.waiting_code,
            ttl_seconds=60,
        )
        lock = pending_challenges._user_lock(user_id)
        await lock.acquire()
        task = asyncio.create_task(
            pending_challenges.read_pending_challenge_data(user_id, state)
        )
        await asyncio.sleep(0)
        task.cancel()
        lock.release()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert token
        assert client.closed is True
        assert client.close_calls == 1
        assert pending_challenges.pending_challenge_count() == 0
        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(scenario())


def test_secret_cleanup_middleware_preserves_concurrent_replacement_challenge(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from guardbot.bot import pending_challenges, runtime
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 524
    old_client = Client()
    replacement_client = Client()
    handler_started = asyncio.Event()
    allow_failure = asyncio.Event()
    monkeypatch.setattr(runtime, "FSMContext", FakeState)

    async def scenario() -> None:
        state = FakeState()
        old_token = await pending_challenges.publish_pending_challenge(
            user_id=user_id,
            mafile_id=42,
            kind="manual_mobile",
            client=old_client,
            state=state,
            fsm_state=SteamChallengeFlow.waiting_code,
            ttl_seconds=60,
        )
        event = SimpleNamespace(
            message=SimpleNamespace(from_user=SimpleNamespace(id=user_id)),
            callback_query=None,
            inline_query=None,
        )

        async def failing_handler(_event, _data):
            handler_started.set()
            await allow_failure.wait()
            raise RuntimeError("handler failed")

        middleware_task = asyncio.create_task(
            runtime.SecretStateCleanupMiddleware()(
                failing_handler,
                event,
                {"state": state},
            )
        )
        await handler_started.wait()
        replacement_token = await pending_challenges.publish_pending_challenge(
            user_id=user_id,
            mafile_id=43,
            kind="manual_mobile",
            client=replacement_client,
            state=state,
            fsm_state=SteamChallengeFlow.waiting_code,
            ttl_seconds=60,
        )
        allow_failure.set()
        with pytest.raises(RuntimeError, match="handler failed"):
            await middleware_task

        try:
            assert old_token != replacement_token
            assert old_client.closed is True
            assert replacement_client.closed is False
            assert pending_challenges.pending_challenge_count() == 1
            assert await state.get_state() == SteamChallengeFlow.waiting_code.state
            assert await state.get_data() == {
                "challenge_id": replacement_token,
                "mafile_id": 43,
                "kind": "manual_mobile",
            }
        finally:
            await pending_challenges.clear_pending_challenges_for_user(
                user_id,
                state,
            )

    asyncio.run(scenario())


def test_ttl_expiry_clears_exact_typed_fsm_binding() -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 525
    client = Client()

    async def scenario() -> None:
        state = FakeState()
        token = await pending_challenges.publish_pending_challenge(
            user_id=user_id,
            mafile_id=42,
            kind="manual_mobile",
            client=client,
            state=state,
            fsm_state=SteamChallengeFlow.waiting_code,
            ttl_seconds=0.01,
        )
        assert token
        await asyncio.sleep(0.05)

        assert client.closed is True
        assert client.close_calls == 1
        assert pending_challenges.pending_challenge_count() == 0
        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(scenario())


def test_ttl_expiry_does_not_clear_replacement_fsm() -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 526
    old_client = Client()
    replacement_client = Client()

    async def scenario() -> None:
        state = FakeState()
        old_token = await pending_challenges.publish_pending_challenge(
            user_id=user_id,
            mafile_id=42,
            kind="manual_mobile",
            client=old_client,
            state=state,
            fsm_state=SteamChallengeFlow.waiting_code,
            ttl_seconds=0.01,
        )
        replacement_token = await pending_challenges.publish_pending_challenge(
            user_id=user_id,
            mafile_id=43,
            kind="manual_mobile",
            client=replacement_client,
            state=state,
            fsm_state=SteamChallengeFlow.waiting_code,
            ttl_seconds=60,
        )
        await asyncio.sleep(0.05)

        try:
            assert old_token != replacement_token
            assert old_client.closed is True
            assert replacement_client.closed is False
            assert pending_challenges.pending_challenge_count() == 1
            assert await state.get_state() == SteamChallengeFlow.waiting_code.state
            assert (await state.get_data())["challenge_id"] == replacement_token
        finally:
            await pending_challenges.clear_pending_challenges_for_user(
                user_id,
                state,
            )

    asyncio.run(scenario())


def test_code_after_expiry_clears_stale_fsm_metadata() -> None:
    from types import SimpleNamespace

    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import steam_challenge
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 527
    client = Client()

    class Message:
        from_user = SimpleNamespace(id=user_id)
        chat = SimpleNamespace(type="private")
        text = "ABCDE"

        def __init__(self) -> None:
            self.answers = []

        async def delete(self) -> None:
            return None

        async def answer(self, text, **_kwargs):
            self.answers.append(text)

    async def scenario() -> None:
        state = FakeState()
        token = await pending_challenges._store_pending_challenge(
            user_id=user_id,
            mafile_id=42,
            kind="manual_mobile",
            client=client,
            ttl_seconds=60,
        )
        await state.set_data(
            {"challenge_id": token, "mafile_id": 42, "kind": "manual_mobile"}
        )
        await state.set_state(SteamChallengeFlow.waiting_code)
        await pending_challenges.remove_pending_challenge(token)

        message = Message()
        await steam_challenge.receive_manual_steam_code(message, state)

        assert client.closed is True
        assert pending_challenges.pending_challenge_count() == 0
        assert await state.get_state() is None
        assert await state.get_data() == {}
        assert message.answers

    asyncio.run(scenario())


def test_pending_registry_has_no_public_lifecycle_bypass_writer() -> None:
    from guardbot.bot import pending_challenges

    assert not hasattr(pending_challenges, "store_pending_challenge")


def test_second_code_while_first_is_checked_does_not_close_leased_client() -> None:
    from types import SimpleNamespace

    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import steam_challenge
    from guardbot.bot.states import SteamChallengeFlow

    user_id = 528
    client = Client()

    class Message:
        from_user = SimpleNamespace(id=user_id)
        chat = SimpleNamespace(type="private")
        text = "ABCDE"

        def __init__(self) -> None:
            self.answers = []

        async def delete(self) -> None:
            return None

        async def answer(self, text, **_kwargs):
            self.answers.append(text)

    async def scenario() -> None:
        state = FakeState()
        token = await pending_challenges.publish_pending_challenge(
            user_id=user_id,
            mafile_id=42,
            kind="manual_mobile",
            client=client,
            state=state,
            fsm_state=SteamChallengeFlow.waiting_code,
            ttl_seconds=60,
        )
        first_lease = await pending_challenges.lease_pending_challenge(
            token,
            user_id=user_id,
            mafile_id=42,
            kind="manual_mobile",
        )
        assert first_lease is not None

        message = Message()
        await steam_challenge.receive_manual_steam_code(message, state)

        try:
            assert client.closed is False
            assert pending_challenges.pending_challenge_count() == 1
            assert await state.get_state() == SteamChallengeFlow.waiting_code.state
            assert (await state.get_data())["challenge_id"] == token
            assert message.answers
            assert "устарел" not in message.answers[-1]
        finally:
            await pending_challenges.clear_pending_challenges_for_user(
                user_id,
                state,
            )

    asyncio.run(scenario())


async def _publish_and_lease_for_other_handler(user_id: int, client, state):
    from guardbot.bot import pending_challenges
    from guardbot.bot.states import SteamChallengeFlow

    token = await pending_challenges.publish_pending_challenge(
        user_id=user_id,
        mafile_id=42,
        kind="manual_mobile",
        client=client,
        state=state,
        fsm_state=SteamChallengeFlow.waiting_code,
        ttl_seconds=60,
    )
    entry = await pending_challenges.lease_pending_challenge(
        token,
        user_id=user_id,
        mafile_id=42,
        kind="manual_mobile",
    )
    assert entry is not None
    return token, entry


async def _assert_leased_challenge_untouched(user_id, client, state, token):
    from guardbot.bot import pending_challenges
    from guardbot.bot.states import SteamChallengeFlow

    try:
        assert client.closed is False
        assert pending_challenges.pending_challenge_count() == 1
        assert await state.get_state() == SteamChallengeFlow.waiting_code.state
        assert (await state.get_data())["challenge_id"] == token
    finally:
        await pending_challenges.clear_pending_challenges_for_user(user_id, state)


def test_exact_cleanup_without_lease_ownership_keeps_leased_challenge() -> None:
    from guardbot.bot import pending_challenges

    user_id = 529
    client = Client()

    async def scenario() -> None:
        state = FakeState()
        token, _entry = await _publish_and_lease_for_other_handler(
            user_id, client, state
        )
        await pending_challenges.clear_owned_pending_challenge(
            token,
            state,
            user_id=user_id,
        )
        await pending_challenges.remove_pending_challenge(token)
        await _assert_leased_challenge_untouched(user_id, client, state, token)

    asyncio.run(scenario())


def test_lease_holder_can_still_clear_its_own_challenge() -> None:
    from guardbot.bot import pending_challenges

    user_id = 530
    client = Client()

    async def scenario() -> None:
        state = FakeState()
        token, entry = await _publish_and_lease_for_other_handler(
            user_id, client, state
        )
        await pending_challenges.clear_owned_pending_challenge(
            token,
            state,
            user_id=user_id,
            expected_entry=entry,
        )
        assert client.closed is True
        assert pending_challenges.pending_challenge_count() == 0
        assert await state.get_state() is None

    asyncio.run(scenario())


def test_second_handler_feedback_failure_keeps_leased_client() -> None:
    from types import SimpleNamespace

    from guardbot.bot.handlers import steam_challenge

    user_id = 531
    client = Client()

    class Message:
        from_user = SimpleNamespace(id=user_id)
        chat = SimpleNamespace(type="private")
        text = "X" * 65

        async def delete(self) -> None:
            return None

        async def answer(self, *_args, **_kwargs):
            raise RuntimeError("feedback failed")

    async def scenario() -> None:
        state = FakeState()
        token, _entry = await _publish_and_lease_for_other_handler(
            user_id, client, state
        )
        with pytest.raises(RuntimeError, match="feedback failed"):
            await steam_challenge.receive_manual_steam_code(Message(), state)
        await _assert_leased_challenge_untouched(user_id, client, state, token)

    asyncio.run(scenario())


def test_second_handler_delete_cancellation_keeps_leased_client(monkeypatch) -> None:
    from types import SimpleNamespace

    from guardbot.bot.handlers import steam_challenge

    user_id = 532
    client = Client()

    async def cancelled_delete(_message):
        raise asyncio.CancelledError

    monkeypatch.setattr(steam_challenge, "delete_secret_message_or_abort", cancelled_delete)

    class Message:
        from_user = SimpleNamespace(id=user_id)
        chat = SimpleNamespace(type="private")
        text = "ABCDE"

    async def scenario() -> None:
        state = FakeState()
        token, _entry = await _publish_and_lease_for_other_handler(
            user_id, client, state
        )
        with pytest.raises(asyncio.CancelledError):
            await steam_challenge.receive_manual_steam_code(Message(), state)
        await _assert_leased_challenge_untouched(user_id, client, state, token)

    asyncio.run(scenario())


def test_secret_cleanup_middleware_keeps_challenge_leased_by_other_handler(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from guardbot.bot import runtime

    user_id = 533
    client = Client()
    monkeypatch.setattr(runtime, "FSMContext", FakeState)

    async def scenario() -> None:
        state = FakeState()
        token, _entry = await _publish_and_lease_for_other_handler(
            user_id, client, state
        )
        event = SimpleNamespace(
            message=SimpleNamespace(from_user=SimpleNamespace(id=user_id)),
            callback_query=None,
            inline_query=None,
        )

        async def failing_handler(_event, _data):
            raise RuntimeError("handler failed")

        with pytest.raises(RuntimeError, match="handler failed"):
            await runtime.SecretStateCleanupMiddleware()(
                failing_handler,
                event,
                {"state": state},
            )
        await _assert_leased_challenge_untouched(user_id, client, state, token)

    asyncio.run(scenario())
