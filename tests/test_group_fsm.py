import asyncio
import inspect
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage


def test_group_commands_escape_account_names_in_html_responses() -> None:
    from guardbot.bot.handlers import bulk_import

    source = inspect.getsource(bulk_import)
    assert 'f"❌ Аккаунт <b>{escape(account_input)}</b> не найден"' in source
    assert 'f"✅ Аккаунт <b>{escape(mafile.account_name)}</b> перемещён:' in source
    assert 'f"❌ Аккаунт <b>{escape(mafile.account_name)}</b> уже без группы"' in source
    assert 'f"✅ Аккаунт <b>{escape(mafile.account_name)}</b> убран из группы' in source


def _state(user_id: int = 987) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )


class _PromptMessage:
    def __init__(self, state: FSMContext, *, error: BaseException | None = None) -> None:
        self.state = state
        self.error = error
        self.states_during_edit: list[str | None] = []

    async def edit_text(self, *_args, **_kwargs) -> None:
        self.states_during_edit.append(await self.state.get_state())
        if self.error is not None:
            raise self.error


class _Callback:
    def __init__(self, state, data: str, *, error: BaseException | None = None) -> None:
        self.from_user = SimpleNamespace(id=987)
        self.data = data
        self.message = _PromptMessage(state, error=error)
        self._guardbot_acknowledged = True


class _InputMessage:
    def __init__(self, text: str = "Group name") -> None:
        self.from_user = SimpleNamespace(id=987)
        self.chat = SimpleNamespace(type="private")
        self.text = text
        self.deleted = False
        self.answers: list[tuple[str, dict]] = []

    async def delete(self) -> None:
        self.deleted = True

    async def answer(self, text: str, **kwargs) -> None:
        self.answers.append((text, kwargs))


class _ScalarResult:
    def __init__(self, value) -> None:
        self.value = value

    def scalar(self):
        return self.value


class _Session:
    def __init__(self, *, scalar_value=0) -> None:
        self.scalar_value = scalar_value
        self.added = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    async def execute(self, _statement):
        return _ScalarResult(self.scalar_value)

    def add(self, value) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.commits += 1


class _FailingRenameState:
    def __init__(self) -> None:
        self.current = None
        self.data = {"group_id": 99, "stale": True}
        self.cleared = False

    async def set_state(self, value) -> None:
        self.current = value.state

    async def set_data(self, _value) -> None:
        raise RuntimeError("storage failed")

    async def clear(self) -> None:
        self.current = None
        self.data = {}
        self.cleared = True

    async def get_state(self):
        return self.current


def test_group_dialogs_no_longer_use_legacy_dispatch() -> None:
    from guardbot.bot.handlers import groups, messages

    sources = inspect.getsource(groups) + inspect.getsource(messages)
    assert "waiting_group_name" not in sources
    assert "waiting_group_rename" not in sources


def test_create_group_sets_state_before_prompt() -> None:
    from guardbot.bot.handlers import groups
    from guardbot.bot.states import GroupFlow

    async def scenario() -> None:
        state = _state()
        callback = _Callback(state, "create_group")

        await groups.callback_create_group(callback, state)

        assert callback.message.states_during_edit == [GroupFlow.waiting_name.state]
        assert await state.get_state() == GroupFlow.waiting_name.state
        assert await state.get_data() == {}

    asyncio.run(scenario())


def test_create_group_clears_competing_legacy_state_before_prompt() -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import groups
    from guardbot.bot.states import GroupFlow

    async def scenario() -> None:
        state = _state()
        callback = _Callback(state, "create_group")
        runtime.user_states[987] = {
            "state": "waiting_trade_notify_interval",
            "mafile_id": 42,
        }

        try:
            await groups.callback_create_group(callback, state)

            assert 987 not in runtime.user_states
            assert await state.get_state() == GroupFlow.waiting_name.state
        finally:
            runtime.user_states.pop(987, None)

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [RuntimeError("failed"), asyncio.CancelledError()])
def test_create_group_rolls_back_state_when_prompt_fails(error: BaseException) -> None:
    from guardbot.bot.handlers import groups

    async def scenario() -> None:
        state = _state()
        callback = _Callback(state, "create_group", error=error)

        with pytest.raises(type(error)):
            await groups.callback_create_group(callback, state)

        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(scenario())


def test_rename_group_stores_only_safe_group_id_before_prompt() -> None:
    from guardbot.bot.handlers import groups
    from guardbot.bot.states import GroupFlow

    async def scenario() -> None:
        state = _state()
        await state.update_data(group_id=99, stale=True)
        callback = _Callback(state, "rename_group_42")

        await groups.callback_rename_group(callback, state)

        assert callback.message.states_during_edit == [GroupFlow.waiting_rename.state]
        assert await state.get_state() == GroupFlow.waiting_rename.state
        assert await state.get_data() == {"group_id": 42}

    asyncio.run(scenario())


def test_rename_group_rolls_back_partial_fsm_initialization() -> None:
    from guardbot.bot.handlers import groups

    async def scenario() -> None:
        state = _FailingRenameState()
        callback = _Callback(state, "rename_group_42")

        with pytest.raises(RuntimeError, match="storage failed"):
            await groups.callback_rename_group(callback, state)

        assert state.cleared is True
        assert state.current is None
        assert state.data == {}
        assert callback.message.states_during_edit == []

    asyncio.run(scenario())


def test_groups_menu_cancels_group_flow_before_render(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import groups
    from guardbot.bot.states import GroupFlow

    class RenderStarted(Exception):
        pass

    monkeypatch.setattr(
        groups,
        "AsyncSessionLocal",
        lambda: (_ for _ in ()).throw(RenderStarted()),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(GroupFlow.waiting_rename)
        await state.set_data({"group_id": 42})
        runtime.user_states[987] = {"state": "waiting_trade_notify_interval"}
        callback = _Callback(state, "groups_menu")

        try:
            with pytest.raises(RenderStarted):
                await groups.callback_groups_menu(callback, state)

            assert await state.get_state() is None
            assert await state.get_data() == {}
            assert 987 not in runtime.user_states
        finally:
            runtime.user_states.pop(987, None)

    asyncio.run(scenario())


@pytest.mark.parametrize("target", ["start", "groups"])
def test_navigation_commands_cancel_group_flow_before_work(monkeypatch, target: str) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import bulk_import, common
    from guardbot.bot.states import GroupFlow

    class WorkStarted(Exception):
        pass

    if target == "start":
        async def fail_save(**_kwargs):
            raise WorkStarted

        monkeypatch.setattr(common, "save_user", fail_save)
    else:
        monkeypatch.setattr(
            bulk_import,
            "AsyncSessionLocal",
            lambda: (_ for _ in ()).throw(WorkStarted()),
        )

    async def scenario() -> None:
        state = _state()
        await state.set_state(GroupFlow.waiting_name)
        runtime.user_states[987] = {"state": "waiting_trade_notify_interval"}
        message = _InputMessage()
        message.chat = SimpleNamespace(type="private")
        message.from_user = SimpleNamespace(
            id=987,
            username="user",
            full_name="User",
        )

        try:
            with pytest.raises(WorkStarted):
                if target == "start":
                    await common.cmd_start_help(message, state)
                else:
                    await bulk_import.cmd_groups(message, state)

            assert await state.get_state() is None
            assert await state.get_data() == {}
            assert 987 not in runtime.user_states
        finally:
            runtime.user_states.pop(987, None)

    asyncio.run(scenario())


@pytest.mark.parametrize("target", ["start", "groups"])
def test_non_private_navigation_commands_cancel_global_group_flow(target: str) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import bulk_import, common
    from guardbot.bot.states import GroupFlow

    async def scenario() -> None:
        state = _state()
        await state.set_state(GroupFlow.waiting_name)
        await state.set_data({"stale": True})
        runtime.user_states[987] = {"state": "waiting_trade_notify_interval"}
        message = _InputMessage()
        message.chat = SimpleNamespace(type="group")
        message.from_user = SimpleNamespace(
            id=987,
            username="user",
            full_name="User",
        )

        try:
            if target == "start":
                await common.cmd_start_help(message, state)
            else:
                await bulk_import.cmd_groups(message, state)

            assert await state.get_state() is None
            assert await state.get_data() == {}
            assert 987 not in runtime.user_states
            assert "личном чате" in message.answers[0][0]
        finally:
            runtime.user_states.pop(987, None)

    asyncio.run(scenario())


def test_change_group_escapes_persisted_names_in_html(monkeypatch) -> None:
    from guardbot.bot.handlers import groups

    class ScalarList:
        def all(self):
            return []

    class Result:
        def scalars(self):
            return ScalarList()

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return Result()

    class RenderMessage:
        chat = SimpleNamespace(type="private")

        def __init__(self) -> None:
            self.texts = []

        async def edit_text(self, text: str, **_kwargs) -> None:
            self.texts.append(text)

    async def owned_mafile(*_args, **_kwargs):
        return SimpleNamespace(
            group_id=7,
            telegram_id=987,
            account_name="<i>account</i>",
        )

    async def owned_group(*_args, **_kwargs):
        return SimpleNamespace(name="<b>INJECTED</b>")

    monkeypatch.setattr(groups, "AsyncSessionLocal", Session)
    monkeypatch.setattr(groups, "get_owned_mafile", owned_mafile)
    monkeypatch.setattr(groups, "get_owned_group", owned_group)

    async def scenario() -> None:
        callback = SimpleNamespace(
            from_user=SimpleNamespace(id=987),
            data="change_group_42",
            message=RenderMessage(),
            _guardbot_acknowledged=True,
        )

        await groups.callback_change_group(callback)

        rendered = callback.message.texts[0]
        assert "&lt;i&gt;account&lt;/i&gt;" in rendered
        assert "&lt;b&gt;INJECTED&lt;/b&gt;" in rendered
        assert "<b>INJECTED</b>" not in rendered

    asyncio.run(scenario())


def test_group_name_handler_creates_group_and_clears_state(monkeypatch) -> None:
    from guardbot.bot.handlers import groups
    from guardbot.bot.states import GroupFlow

    session = _Session(scalar_value=3)
    monkeypatch.setattr(groups, "AsyncSessionLocal", lambda: session)

    async def scenario() -> None:
        state = _state()
        await state.set_state(GroupFlow.waiting_name)
        message = _InputMessage("  My group  ")

        await groups.process_group_name(message, state)

        assert message.deleted is True
        assert session.commits == 1
        assert len(session.added) == 1
        assert session.added[0].telegram_id == 987
        assert session.added[0].name == "My group"
        assert session.added[0].sort_order == 4
        assert await state.get_state() is None
        assert "My group" in message.answers[0][0]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("group_name", "error_text"),
    [
        ("   ", "не может быть пустым"),
        ("x" * 101, "не должно превышать 100"),
    ],
)
def test_group_name_validation_keeps_flow_active(
    monkeypatch,
    group_name: str,
    error_text: str,
) -> None:
    from guardbot.bot.handlers import groups
    from guardbot.bot.states import GroupFlow

    monkeypatch.setattr(
        groups,
        "AsyncSessionLocal",
        lambda: pytest.fail("invalid input must not open a database session"),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(GroupFlow.waiting_name)
        message = _InputMessage(group_name)

        await groups.process_group_name(message, state)

        assert await state.get_state() == GroupFlow.waiting_name.state
        assert error_text in message.answers[0][0]

    asyncio.run(scenario())


def test_group_name_is_escaped_in_html_confirmation(monkeypatch) -> None:
    from guardbot.bot.handlers import groups
    from guardbot.bot.states import GroupFlow

    session = _Session()
    monkeypatch.setattr(groups, "AsyncSessionLocal", lambda: session)

    async def scenario() -> None:
        state = _state()
        await state.set_state(GroupFlow.waiting_name)
        message = _InputMessage("<b>not markup</b>")

        await groups.process_group_name(message, state)

        assert "&lt;b&gt;not markup&lt;/b&gt;" in message.answers[0][0]

    asyncio.run(scenario())


def test_group_rename_rechecks_ownership_and_clears_missing_group(monkeypatch) -> None:
    from guardbot.bot.handlers import groups
    from guardbot.bot.states import GroupFlow

    session = _Session()

    async def missing_group(*_args, **_kwargs):
        return None

    monkeypatch.setattr(groups, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(groups, "get_owned_group", missing_group)

    async def scenario() -> None:
        state = _state()
        await state.set_state(GroupFlow.waiting_rename)
        await state.update_data(group_id=42)
        message = _InputMessage("New name")

        await groups.process_group_rename(message, state)

        assert session.commits == 0
        assert await state.get_state() is None
        assert message.answers == [("❌ Группа не найдена", {})]

    asyncio.run(scenario())


def test_dispatcher_rejects_cross_chat_group_flow_input(monkeypatch) -> None:
    from datetime import UTC, datetime

    from aiogram import Bot
    from aiogram.methods import DeleteMessage, SendMessage
    from aiogram.types import Chat, Message, Update, User

    from guardbot.bot import runtime
    from guardbot.bot.handlers import groups
    from guardbot.bot.states import GroupFlow

    class Result:
        @staticmethod
        def scalar_one_or_none():
            return None

    class LockSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            return Result()

    class FakeBot(Bot):
        def __init__(self) -> None:
            super().__init__(token="42:TEST")
            self.calls = []

        async def __call__(self, method, request_timeout=None):
            self.calls.append(method)
            if isinstance(method, SendMessage):
                return Message(
                    message_id=100 + len(self.calls),
                    date=datetime.now(UTC),
                    chat=Chat(id=int(method.chat_id), type="group"),
                    from_user=User(
                        id=self.id,
                        is_bot=True,
                        first_name="GuardBot",
                    ),
                    text=method.text,
                )
            if isinstance(method, DeleteMessage):
                raise AssertionError("cross-chat group input must not be deleted")
            raise AssertionError(f"Unexpected Bot API method: {method!r}")

    database_calls = []

    def forbidden_group_session():
        database_calls.append(True)
        raise AssertionError("cross-chat input must not access group storage")

    monkeypatch.setattr(runtime, "AsyncSessionLocal", LockSession)
    monkeypatch.setattr(groups, "AsyncSessionLocal", forbidden_group_session)

    async def scenario() -> None:
        bot = FakeBot()
        state = runtime.dp.fsm.get_context(bot=bot, chat_id=987, user_id=987)
        try:
            for index, flow_state in enumerate(
                (GroupFlow.waiting_name, GroupFlow.waiting_rename),
                start=1,
            ):
                await state.set_state(flow_state)
                expected_data = (
                    {"group_id": 42}
                    if flow_state == GroupFlow.waiting_rename
                    else {}
                )
                await state.set_data(expected_data)

                await runtime.dp.feed_update(
                    bot,
                    Update(
                        update_id=index,
                        message=Message(
                            message_id=index,
                            date=datetime.now(UTC),
                            chat=Chat(id=-100123, type="group"),
                            from_user=User(
                                id=987,
                                is_bot=False,
                                first_name="Test",
                            ),
                            text="accidental group text",
                        ),
                    ),
                )

                assert await state.get_state() == flow_state.state
                assert await state.get_data() == expected_data

            assert database_calls == []
            sent_texts = [
                method.text for method in bot.calls if isinstance(method, SendMessage)
            ]
            assert len(sent_texts) == 2
            assert all("личном чате" in text for text in sent_texts)
            assert not any(isinstance(method, DeleteMessage) for method in bot.calls)
        finally:
            await state.clear()
            await bot.session.close()

    asyncio.run(scenario())
