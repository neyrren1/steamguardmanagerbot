import asyncio
import inspect
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage


def _state(user_id: int = 987) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
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


class _Session:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def commit(self) -> None:
        self.commits += 1


class _PromptMessage:
    chat = SimpleNamespace(type="private")

    def __init__(self, state: FSMContext, error: BaseException | None = None) -> None:
        self.state = state
        self.error = error
        self.states_during_edit = []

    async def edit_text(self, *_args, **_kwargs) -> None:
        self.states_during_edit.append(await self.state.get_state())
        if self.error is not None:
            raise self.error


class _Callback:
    def __init__(self, state: FSMContext, error: BaseException | None = None) -> None:
        self.from_user = SimpleNamespace(id=987)
        self.data = "change_note_42"
        self.message = _PromptMessage(state, error)
        self._guardbot_acknowledged = True


class _InputMessage:
    def __init__(self, text: str, chat_type: str = "private") -> None:
        self.from_user = SimpleNamespace(id=987)
        self.chat = SimpleNamespace(type=chat_type)
        self.text = text
        self.deleted = False
        self.answers = []

    async def delete(self) -> None:
        self.deleted = True

    async def answer(self, text: str, **kwargs):
        self.answers.append((text, kwargs))
        return SimpleNamespace()


def test_note_dialog_no_longer_uses_legacy_dispatch() -> None:
    from guardbot.bot.handlers import accounts, messages

    sources = inspect.getsource(accounts) + inspect.getsource(messages)
    assert '"waiting_note"' not in sources


def test_note_prompt_replaces_competing_workflows_and_sets_state_before_delivery(
    monkeypatch,
) -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import NoteFlow

    session = _Session()
    mafile = SimpleNamespace(id=42, account_name="account", notes=None)

    async def owned(*_args, **_kwargs):
        return mafile

    monkeypatch.setattr(accounts, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(accounts, "get_owned_mafile", owned)

    async def scenario() -> None:
        state = _state()
        client = _ChallengeClient()
        await _publish_challenge(987, state, client)
        await state.update_data(stale=True)
        callback = _Callback(state)
        try:
            await accounts.callback_change_note(callback, state)

            assert client.close_calls == 1
            assert pending_challenges.current_pending_challenge_token(987) is None
            assert callback.message.states_during_edit == [NoteFlow.waiting_note.state]
            assert await state.get_state() == NoteFlow.waiting_note.state
            assert await state.get_data() == {"mafile_id": 42}
        finally:
            await pending_challenges.clear_pending_challenges_for_user(987, state)

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [RuntimeError("failed"), asyncio.CancelledError()])
def test_note_prompt_failure_rolls_back_state(monkeypatch, error: BaseException) -> None:
    from guardbot.bot.handlers import accounts

    session = _Session()

    async def owned(*_args, **_kwargs):
        return SimpleNamespace(id=42, account_name="account", notes=None)

    monkeypatch.setattr(accounts, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(accounts, "get_owned_mafile", owned)

    async def scenario() -> None:
        state = _state()
        callback = _Callback(state, error)

        with pytest.raises(type(error)):
            await accounts.callback_change_note(callback, state)

        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(scenario())


def test_note_input_rejects_non_private_chat_before_delete_or_database(monkeypatch) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import NoteFlow

    monkeypatch.setattr(
        accounts,
        "AsyncSessionLocal",
        lambda: pytest.fail("cross-chat input must not open a database session"),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(NoteFlow.waiting_note)
        await state.set_data({"mafile_id": 42})
        message = _InputMessage("private note", chat_type="group")

        await accounts.process_account_note(message, state)

        assert message.deleted is False
        assert await state.get_state() == NoteFlow.waiting_note.state
        assert await state.get_data() == {"mafile_id": 42}
        assert "личном чате" in message.answers[0][0]

    asyncio.run(scenario())


def test_note_input_encrypts_and_clears_terminal_state(monkeypatch) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import NoteFlow

    session = _Session()
    mafile = SimpleNamespace(id=42, account_name="<b>account</b>", notes=None)
    settings_updates = []

    async def owned(*_args, **_kwargs):
        return mafile

    async def update_settings(*args):
        settings_updates.append(args)

    monkeypatch.setattr(accounts, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(accounts, "get_owned_mafile", owned)
    monkeypatch.setattr(accounts, "encrypt_value", lambda value: f"encrypted:{value}")
    monkeypatch.setattr(accounts, "update_account_settings_message", update_settings)

    async def scenario() -> None:
        state = _state()
        await state.set_state(NoteFlow.waiting_note)
        await state.set_data({"mafile_id": 42})
        message = _InputMessage("<i>private note</i>")

        await accounts.process_account_note(message, state)

        assert message.deleted is True
        assert mafile.notes == "encrypted:<i>private note</i>"
        assert session.commits == 1
        assert await state.get_state() is None
        assert await state.get_data() == {}
        assert len(settings_updates) == 1
        confirmation = message.answers[-1][0]
        assert "&lt;b&gt;account&lt;/b&gt;" in confirmation
        assert "&lt;i&gt;private note&lt;/i&gt;" in confirmation
        assert "<i>private note</i>" not in confirmation

    asyncio.run(scenario())


def test_note_deletion_failure_aborts_before_fsm_data_and_database(monkeypatch) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import NoteFlow

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
        await state.set_state(NoteFlow.waiting_note)
        await state.set_data({"mafile_id": 42})
        message = _InputMessage("private note")
        original_get_data = state.get_data

        async def forbidden_get_data():
            raise AssertionError("FSM data must not be read after deletion failure")

        state.get_data = forbidden_get_data
        try:
            await accounts.process_account_note(message, state)
        finally:
            state.get_data = original_get_data

        assert await state.get_state() == NoteFlow.waiting_note.state
        assert await state.get_data() == {"mafile_id": 42}

    asyncio.run(scenario())


def test_note_flow_is_classified_as_secret_input() -> None:
    from guardbot.bot import runtime
    from guardbot.bot.states import NoteFlow

    assert NoteFlow.waiting_note.state in runtime._SECRET_FSM_STATES


def test_direct_note_command_deletion_failure_aborts_before_database(monkeypatch) -> None:
    from guardbot.bot.handlers import commands

    async def deletion_failed(_message):
        return False

    monkeypatch.setattr(commands, "delete_secret_message_or_abort", deletion_failed)
    monkeypatch.setattr(
        commands,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(AssertionError("database must not open")),
    )

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=987),
        chat=SimpleNamespace(type="private"),
    )

    asyncio.run(
        commands.cmd_note(
            message,
            SimpleNamespace(args="account top-secret"),
        )
    )


def test_note_non_text_fallback_precedes_legacy_catch_all(monkeypatch) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import NoteFlow

    script = """
import tgbot
from guardbot.bot import routing
from guardbot.bot.handlers.accounts import reject_note_non_text
from guardbot.bot.handlers.messages import handle_messages
routing.setup_routers(tgbot.dp)  # what the launcher's main() does
callbacks = [handler.callback for handler in routing.ordered_handlers(tgbot.dp, "message")]
assert callbacks.index(reject_note_non_text) < callbacks.index(handle_messages)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    class DocumentMessage:
        from_user = SimpleNamespace(id=987)
        chat = SimpleNamespace(type="private")

        def __init__(self) -> None:
            self.answers = []

        async def answer(self, text: str, **kwargs) -> None:
            self.answers.append((text, kwargs))

    async def already_deleted(_message):
        return True

    monkeypatch.setattr(accounts, "delete_secret_message_or_abort", already_deleted)

    async def scenario() -> None:
        state = _state()
        await state.set_state(NoteFlow.waiting_note)
        await state.set_data({"mafile_id": 42})
        message = DocumentMessage()

        await accounts.reject_note_non_text(message, state)

        assert await state.get_state() == NoteFlow.waiting_note.state
        assert await state.get_data() == {"mafile_id": 42}
        assert "текстом" in message.answers[0][0]

    asyncio.run(scenario())


@pytest.mark.parametrize("account_exists", [True, False])
def test_note_cancel_button_clears_typed_flow_before_render(
    monkeypatch,
    account_exists: bool,
) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import NoteFlow

    async def scenario() -> None:
        state = _state()
        await state.set_state(NoteFlow.waiting_note)
        await state.set_data({"mafile_id": 42})
        render_states = []
        callback = _Callback(state)
        callback.data = "cancel_state_to_settings_42"

        async def feedback(*_args, **_kwargs):
            return None

        async def owned(*_args, **_kwargs):
            if account_exists:
                return SimpleNamespace(id=42, account_name="account", notes=None)
            return None

        async def render(*_args, **_kwargs):
            render_states.append(await state.get_state())

        monkeypatch.setattr(accounts, "callback_feedback", feedback)
        monkeypatch.setattr(accounts, "AsyncSessionLocal", lambda: _Session())
        monkeypatch.setattr(accounts, "get_owned_mafile", owned)
        monkeypatch.setattr(accounts, "update_account_settings_message", render)

        await accounts.callback_cancel_state_to_settings(callback, state)

        assert await state.get_state() is None
        assert await state.get_data() == {}
        if account_exists:
            assert render_states == [None]
        else:
            assert callback.message.states_during_edit == [None]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "callback_data",
    ["cancel_state_to_settings_", "cancel_state_to_settings_invalid"],
)
def test_malformed_note_cancel_still_clears_typed_flow(
    monkeypatch,
    callback_data: str,
) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import NoteFlow

    async def scenario() -> None:
        state = _state()
        await state.set_state(NoteFlow.waiting_note)
        await state.set_data({"mafile_id": 42})
        callback = _Callback(state)
        callback.data = callback_data
        feedbacks = []

        async def feedback(*args, **kwargs):
            feedbacks.append((args, kwargs))

        monkeypatch.setattr(accounts, "callback_feedback", feedback)
        monkeypatch.setattr(
            accounts,
            "AsyncSessionLocal",
            lambda: pytest.fail("malformed cancellation must not access the database"),
        )

        await accounts.callback_cancel_state_to_settings(callback, state)

        assert await state.get_state() is None
        assert await state.get_data() == {}
        assert len(feedbacks) == 1

    asyncio.run(scenario())
