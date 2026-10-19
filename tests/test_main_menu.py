import asyncio
from types import SimpleNamespace

import pytest
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    DisabledButton,
    InputRichBlockButtons,
    InputRichBlockDetails,
    InputRichBlockParagraph,
    InputRichBlockSectionHeading,
    InputRichMessage,
    RichTextButton,
)

from guardbot.bot.main_menu import (
    build_main_menu_legacy,
    build_main_menu_rich,
    build_main_menu_view,
    edit_main_menu,
    send_main_menu,
)


def _fsm_context(user_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=42, chat_id=user_id, user_id=user_id),
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


def _block_buttons(message: InputRichMessage):
    return [
        button
        for block in message.blocks or []
        if isinstance(block, InputRichBlockButtons)
        for button in block.buttons
    ]


def _inline_buttons(message: InputRichMessage):
    buttons = []
    for block in message.blocks or []:
        text = getattr(block, "text", None)
        values = text if isinstance(text, list) else [text]
        buttons.extend(value.button for value in values if isinstance(value, RichTextButton))
    return buttons


def _account(
    account_id: int,
    *,
    name: str | None = None,
    guard: bool = False,
    logged: bool = False,
    confirmations: bool = False,
    pinned: bool = False,
    sort_order: int = 0,
):
    return SimpleNamespace(
        id=account_id,
        account_name=name or f"account-{account_id}",
        shared_secret="secret" if guard else None,
        identity_secret="identity" if confirmations else None,
        access_token="access" if logged else None,
        refresh_token="refresh" if logged else None,
        password="password" if logged else None,
        is_pinned=pinned,
        sort_order=sort_order,
    )


def test_main_menu_view_counts_accounts_without_exposing_secrets() -> None:
    view = build_main_menu_view(
        "<Admin & User>",
        [
            _account(10, guard=True, logged=True, confirmations=True),
            _account(20, guard=True, pinned=True),
            _account(30),
        ],
    )

    assert view.display_name == "<Admin & User>"
    assert view.account_ids == (20, 10, 30)
    assert view.accounts_with_2fa == 2
    assert view.accounts_logged == 1


def test_rich_main_menu_adapts_account_actions() -> None:
    zero_buttons = _block_buttons(
        build_main_menu_rich(build_main_menu_view("User", []))
    )
    assert {button.callback_data for button in zero_buttons if button.callback_data} >= {
        "add_account",
        "my_accounts",
        "settings",
        "bulk_import_menu",
        "mafile_instruction",
    }

    one_buttons = _block_buttons(
        build_main_menu_rich(build_main_menu_view("User", [_account(42)]))
    )
    assert any(button.callback_data == "account_detail_42" for button in one_buttons)
    assert any(button.callback_data == "add_account" for button in one_buttons)

    many_message = build_main_menu_rich(
        build_main_menu_view("User", [_account(1), _account(2)])
    )
    many_buttons = _block_buttons(many_message)
    assert any(button.callback_data == "my_accounts" for button in many_buttons)
    assert any(button.callback_data == "groups_menu" for button in many_buttons)
    assert any(
        button.switch_inline_query_current_chat == "/gc "
        and button.style is None
        for button in many_buttons
    )

    inline_callbacks = {
        button.callback_data
        for button in _inline_buttons(many_message)
        if button.callback_data
    }
    assert inline_callbacks == {"info", "instruction"}


def test_main_menu_uses_account_list_order_for_three_quick_accounts() -> None:
    view = build_main_menu_view(
        "User",
        [
            _account(30, name="third", sort_order=2),
            _account(20, name="second", pinned=True, sort_order=5),
            _account(10, name="first", pinned=True, sort_order=1),
            _account(40, name="fourth", sort_order=0),
        ],
    )

    assert view.account_ids == (10, 20, 40, 30)
    assert [account.account_id for account in view.quick_accounts] == [10, 20, 40]


def test_rich_main_menu_has_compact_navigation_and_account_shortcuts() -> None:
    message = build_main_menu_rich(
        build_main_menu_view(
            "User",
            [
                _account(1, name="primary", guard=True, logged=True, confirmations=True),
                _account(2, name="no-guard"),
                _account(3, name="guard-only", guard=True),
                _account(4, name="hidden"),
            ],
            telegram_id=123456789,
        )
    )
    blocks = message.blocks or []
    payload = message.model_dump_json()

    assert "Главное меню" not in payload
    assert not any(isinstance(block, InputRichBlockSectionHeading) for block in blocks)
    introduction = blocks[0]
    assert isinstance(introduction, InputRichBlockParagraph)
    introduction_payload = introduction.model_dump_json()
    assert "👋 Привет, " in introduction_payload
    assert '"type":"italic","text":"User"' in introduction_payload
    assert (
        "🎮 Steam Guard Manager Bot - ваш надежный помощник для управления "
        "Steam аккаунтами и Steam Guard кодами."
    ) in introduction_payload
    assert "📊 Ваша статистика:" in introduction_payload
    assert "📊 Ваша статистика:\\n\\n" not in introduction_payload
    assert "📊 Ваша статистика:\\n" in introduction_payload
    assert "├─ UID: " in introduction_payload
    assert '"type":"code","text":"123456789"' in introduction_payload
    assert '"type":"italic","text":"123456789"' not in introduction_payload
    assert "├─ Аккаунтов: 4" in introduction_payload
    assert "├─ С 2FA: 2" in introduction_payload
    assert "└─ Авторизовано: 1" in introduction_payload
    assert "С подтверждениями" not in introduction_payload
    assert "Закреплено" not in introduction_payload
    assert "требуют настроенный прокси" not in payload
    assert "Быстрый доступ" not in payload

    commands_index = next(
        index
        for index, block in enumerate(blocks)
        if isinstance(block, InputRichBlockDetails)
        and "Список быстрых команд" in block.model_dump_json()
    )
    assert commands_index == 1

    rows = [
        block.buttons
        for block in blocks
        if isinstance(block, InputRichBlockButtons)
    ]
    assert all(
        block.align is None
        for block in blocks
        if isinstance(block, InputRichBlockButtons)
    )
    assert [button.callback_data for button in rows[0]] == ["add_account"]
    assert [button.callback_data for button in rows[1]] == ["my_accounts"]

    account_rows = [
        row
        for row in rows
        if row and (row[0].callback_data or "").startswith("account_detail_")
    ]
    assert len(account_rows) == 3
    assert all(len(str(row[0].text)) <= 10 for row in account_rows)
    assert {str(row[2].text) for row in account_rows} <= {"Подтв.", "Подтв. —"}
    assert [row[0].callback_data for row in account_rows] == [
        "account_detail_1",
        "account_detail_2",
        "account_detail_3",
    ]
    assert [button.callback_data for button in account_rows[0]] == [
        "account_detail_1",
        "action_get_code_1",
        "confirmations_menu_1",
    ]
    assert isinstance(account_rows[1][1].disabled, DisabledButton)
    assert isinstance(account_rows[1][2].disabled, DisabledButton)
    assert account_rows[2][1].callback_data == "action_get_code_3"
    assert isinstance(account_rows[2][2].disabled, DisabledButton)

    guard_codes_index = next(
        index
        for index, block in enumerate(blocks)
        if isinstance(block, InputRichBlockButtons)
        and block.buttons[0].switch_inline_query_current_chat == "/gc "
    )
    settings_index = next(
        index
        for index, block in enumerate(blocks)
        if isinstance(block, InputRichBlockButtons)
        and block.buttons[0].callback_data == "settings"
    )
    groups_index = next(
        index
        for index, block in enumerate(blocks)
        if isinstance(block, InputRichBlockButtons)
        and block.buttons[0].callback_data == "groups_menu"
    )

    import_row = next(
        row for row in rows if row[0].callback_data == "bulk_import_menu"
    )
    assert [button.callback_data for button in import_row] == [
        "bulk_import_menu",
        "mafile_instruction",
    ]
    settings_row = next(row for row in rows if row[0].callback_data == "settings")
    assert [button.callback_data for button in settings_row] == ["settings"]
    imports_index = next(
        index
        for index, block in enumerate(blocks)
        if isinstance(block, InputRichBlockButtons)
        and block.buttons[0].callback_data == "bulk_import_menu"
    )
    assert commands_index < imports_index < groups_index < settings_index < guard_codes_index

    styled_buttons = [
        button
        for button in _block_buttons(message)
        if button.style is not None
    ]
    assert len(styled_buttons) == 1
    assert styled_buttons[0].callback_data == "add_account"
    assert styled_buttons[0].style == ButtonStyle.SUCCESS


def test_rich_main_menu_buttons_have_one_valid_action() -> None:
    message = build_main_menu_rich(
        build_main_menu_view(
            "User",
            [_account(1, guard=True, logged=True, confirmations=True)],
        )
    )
    buttons = _block_buttons(message) + _inline_buttons(message)

    for button in buttons:
        actions = (
            button.callback_data,
            button.switch_inline_query_current_chat,
            button.disabled,
        )
        assert sum(action is not None for action in actions) == 1
        if button.callback_data is not None:
            assert len(button.callback_data.encode("utf-8")) <= 64


def test_main_menu_preserves_quick_command_discovery() -> None:
    view = build_main_menu_view("User", [_account(42)])

    rich_payload = build_main_menu_rich(view).model_dump_json()
    legacy_text, _ = build_main_menu_legacy(view)

    for command in ("/gc", "/add", "/conf", "/trades", "/inv", "/login"):
        assert command in rich_payload
        assert command in legacy_text


def test_legacy_main_menu_escapes_telegram_display_name() -> None:
    text, keyboard = build_main_menu_legacy(
        build_main_menu_view("<Admin & User>", [], telegram_id=123456789)
    )

    assert "&lt;Admin &amp; User&gt;" in text
    assert "<Admin & User>" not in text
    assert "Главное меню" not in text
    assert "Steam Guard Manager Bot" in text
    assert "Ваша статистика:\n├─ UID" in text
    assert "UID: <code>123456789</code>" in text
    assert keyboard.inline_keyboard


def test_legacy_main_menu_keeps_requested_navigation_order() -> None:
    _, keyboard = build_main_menu_legacy(
        build_main_menu_view(
            "User",
            [_account(1, name="one"), _account(2, name="two")],
            telegram_id=123456789,
        )
    )
    actions = [
        button.callback_data or button.switch_inline_query_current_chat
        for row in keyboard.inline_keyboard
        for button in row
    ]

    assert actions.index("bulk_import_menu") < actions.index("groups_menu")
    assert actions.index("groups_menu") < actions.index("settings")
    assert actions.index("settings") < actions.index("/gc ")


def test_send_main_menu_uses_rich_message() -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.rich_calls = []
            self.legacy_calls = []

        async def send_rich_message(self, **kwargs):
            self.rich_calls.append(kwargs)
            return "rich"

        async def send_message(self, **kwargs):
            self.legacy_calls.append(kwargs)
            return "legacy"

    bot = FakeBot()
    view = build_main_menu_view("User", [])

    result = asyncio.run(
        send_main_menu(
            bot,
            chat_id=123,
            view=view,
            message_thread_id=456,
            business_connection_id="business-123",
        )
    )

    assert result == "rich"
    assert bot.rich_calls[0]["chat_id"] == 123
    assert bot.rich_calls[0]["message_thread_id"] == 456
    assert bot.rich_calls[0]["business_connection_id"] == "business-123"
    assert isinstance(bot.rich_calls[0]["rich_message"], InputRichMessage)
    assert bot.legacy_calls == []


def test_send_main_menu_falls_back_to_legacy_message() -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.legacy_calls = []

        async def send_rich_message(self, **kwargs):
            raise TelegramBadRequest(method=object(), message="rich unavailable")

        async def send_message(self, **kwargs):
            self.legacy_calls.append(kwargs)
            return "legacy"

    bot = FakeBot()
    result = asyncio.run(
        send_main_menu(
            bot,
            chat_id=123,
            view=build_main_menu_view("User", []),
            message_thread_id=456,
            business_connection_id="business-123",
        )
    )

    assert result == "legacy"
    assert bot.legacy_calls[0]["chat_id"] == 123
    assert bot.legacy_calls[0]["message_thread_id"] == 456
    assert bot.legacy_calls[0]["business_connection_id"] == "business-123"
    assert bot.legacy_calls[0]["parse_mode"] == "HTML"


def test_edit_main_menu_uses_rich_message_and_has_legacy_fallback() -> None:
    class RichMessage:
        def __init__(self) -> None:
            self.calls = []

        async def edit_text(self, **kwargs):
            self.calls.append(kwargs)
            return "edited"

    rich_message = RichMessage()
    view = build_main_menu_view("User", [_account(1)])

    assert asyncio.run(edit_main_menu(rich_message, view)) == "edited"
    assert isinstance(rich_message.calls[0]["rich_message"], InputRichMessage)
    assert rich_message.calls[0]["text"] is None

    class LegacyFallbackMessage:
        def __init__(self) -> None:
            self.calls = []

        async def edit_text(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise TelegramBadRequest(method=object(), message="rich unavailable")
            return "legacy"

    fallback_message = LegacyFallbackMessage()
    assert asyncio.run(edit_main_menu(fallback_message, view)) == "legacy"
    assert fallback_message.calls[1]["parse_mode"] == "HTML"
    assert "reply_markup" in fallback_message.calls[1]


def test_rich_main_menu_can_open_existing_plain_info_screen() -> None:
    from guardbot.bot.handlers import accounts

    class Message:
        def __init__(self) -> None:
            self.rich_message = build_main_menu_rich(build_main_menu_view("User", []))
            self.calls = []

        async def edit_text(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    class Callback:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=123, username="tester")
            self.message = Message()
            self.answers = 0

        async def answer(self):
            self.answers += 1

    callback = Callback()

    asyncio.run(accounts.callback_info(callback))

    args, kwargs = callback.message.calls[0]
    assert "ДЕТАЛЬНАЯ ИНФОРМАЦИЯ" in args[0]
    assert kwargs["parse_mode"] == "HTML"
    assert "rich_message" not in kwargs
    assert callback.answers == 1


def test_my_accounts_callback_acknowledges_the_query(monkeypatch) -> None:
    from guardbot.bot.handlers import accounts

    calls = []

    async def show_accounts_page(callback, page):
        calls.append((callback, page))

    class Callback:
        def __init__(self) -> None:
            self.answers = 0

        async def answer(self):
            self.answers += 1

    monkeypatch.setattr(accounts, "show_accounts_page", show_accounts_page)
    callback = Callback()

    asyncio.run(accounts.callback_my_accounts(callback))

    assert calls == [(callback, 0)]
    assert callback.answers == 1


def test_cancel_account_addition_acknowledges_once_with_notice(monkeypatch) -> None:
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import AccountAddFlow

    page_calls = []

    async def show_accounts_page(callback, page):
        page_calls.append((callback, page))

    class Message:
        def __init__(self) -> None:
            self.feedback: list[str] = []

        async def answer(self, text, **kwargs) -> None:
            self.feedback.append(text)

    class Callback:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=987)
            self.message = Message()
            self.answers = []

        async def answer(self, text=None):
            self.answers.append(text)

    monkeypatch.setattr(accounts, "show_accounts_page", show_accounts_page)
    callback = Callback()
    state = _fsm_context(callback.from_user.id)

    async def scenario() -> None:
        await state.set_state(AccountAddFlow.waiting_credentials)
        await accounts.callback_cancel_state_to_accounts(callback, state)
        assert await state.get_state() is None

    asyncio.run(scenario())

    assert callback.answers == [None]
    assert callback.message.feedback == ["❌ Добавление отменено"]
    assert page_calls == [(callback, 0)]


def test_add_account_button_sets_state_before_showing_credentials_prompt() -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import accounts
    from guardbot.bot.states import AccountAddFlow

    user_id = 987
    state = _fsm_context(user_id)
    client = _ChallengeClient()

    class Message:
        chat = SimpleNamespace(type="private")

        async def edit_text(self, *args, **kwargs):
            assert await state.get_state() == AccountAddFlow.waiting_credentials.state
            assert await state.get_data() == {}
            assert client.close_calls == 1
            assert pending_challenges.current_pending_challenge_token(user_id) is None

    class Callback:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=user_id)
            self.message = Message()
            self.answers = 0

        async def answer(self):
            self.answers += 1

    callback = Callback()

    async def scenario() -> None:
        await _publish_challenge(user_id, state, client)
        try:
            await accounts.callback_add_account(callback, state)
            assert callback.answers == 1
            assert client.close_calls == 1
        finally:
            await pending_challenges.clear_pending_challenges_for_user(user_id, state)

    asyncio.run(scenario())


def test_start_handler_sends_rich_main_menu(monkeypatch) -> None:
    from guardbot.bot.handlers import common

    accounts = [_account(7, guard=True, logged=True)]
    sent_views = []

    class Result:
        def scalars(self):
            return self

        def all(self):
            return accounts

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            return Result()

    async def save_user(**kwargs):
        return None

    async def send_main_menu(
        bot,
        chat_id,
        view,
        message_thread_id=None,
        business_connection_id=None,
    ):
        sent_views.append(
            (bot, chat_id, view, message_thread_id, business_connection_id)
        )

    monkeypatch.setattr(common, "AsyncSessionLocal", Session)
    monkeypatch.setattr(common, "save_user", save_user)
    monkeypatch.setattr(common, "send_main_menu", send_main_menu)

    user = SimpleNamespace(
        id=123,
        username="tester",
        full_name="Test User",
    )
    message = SimpleNamespace(
        bot=object(),
        chat=SimpleNamespace(id=456),
        from_user=user,
        message_thread_id=789,
        business_connection_id="business-123",
    )

    asyncio.run(common.cmd_start_help(message))

    assert len(sent_views) == 1
    _, chat_id, view, thread_id, business_connection_id = sent_views[0]
    assert chat_id == 456
    assert thread_id == 789
    assert business_connection_id == "business-123"
    assert view.display_name == "Test User"
    assert view.account_ids == (7,)


def test_back_to_start_edits_message_to_rich_main_menu(monkeypatch) -> None:
    from guardbot.bot import runtime

    accounts = [_account(1), _account(2, guard=True)]
    edited_views = []

    class Result:
        def scalars(self):
            return self

        def all(self):
            return accounts

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            return Result()

    async def edit_main_menu(message, view):
        edited_views.append((message, view))

    class State:
        async def get_state(self):
            return None

        async def clear(self):
            raise AssertionError("state is already empty")

    class Callback:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(
                id=321,
                username="tester",
                full_name="Back User",
            )
            self.message = object()
            self.answers = 0

        async def answer(self):
            self.answers += 1

    callback = Callback()
    monkeypatch.setattr(runtime, "AsyncSessionLocal", Session)
    monkeypatch.setattr(runtime, "edit_main_menu", edit_main_menu)

    asyncio.run(runtime.process_back_to_start(callback, State()))

    assert callback.answers == 1
    assert len(edited_views) == 1
    _, view = edited_views[0]
    assert view.display_name == "Back User"
    assert view.account_ids == (1, 2)


def test_cancel_state_returns_to_rich_main_menu(monkeypatch) -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import common

    accounts = [_account(5, guard=True)]
    edited_views = []

    class Result:
        def scalars(self):
            return self

        def all(self):
            return accounts

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            return Result()

    async def edit_main_menu(message, view):
        edited_views.append((message, view))

    class Message:
        chat = SimpleNamespace(type="private")

        def __init__(self) -> None:
            self.feedback: list[str] = []

        async def answer(self, text, **kwargs) -> None:
            self.feedback.append(text)

    class Callback:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(
                id=654,
                username="tester",
                full_name="Cancel User",
            )
            self.message = Message()
            self.answers = []

        async def answer(self, text=None):
            self.answers.append(text)

    callback = Callback()
    client = _ChallengeClient()
    challenge_state = _fsm_context(callback.from_user.id)
    monkeypatch.setattr(common, "AsyncSessionLocal", Session)
    monkeypatch.setattr(common, "edit_main_menu", edit_main_menu)

    async def scenario() -> None:
        await _publish_challenge(callback.from_user.id, challenge_state, client)
        try:
            await common.callback_cancel_state_to_start(callback)
            assert client.close_calls == 1
            assert (
                pending_challenges.current_pending_challenge_token(
                    callback.from_user.id
                )
                is None
            )
        finally:
            await pending_challenges.clear_pending_challenges_for_user(
                callback.from_user.id,
                challenge_state,
            )

    asyncio.run(scenario())

    assert client.close_calls == 1
    assert callback.answers == [None]
    assert callback.message.feedback == ["❌ Действие отменено"]
    assert len(edited_views) == 1
    _, view = edited_views[0]
    assert view.display_name == "Cancel User"
    assert view.account_ids == (5,)


def test_main_menu_command_does_not_load_account_metadata_in_group(monkeypatch) -> None:
    from guardbot.bot.handlers import common

    calls = {"save": 0, "session": 0}

    async def save_user(**kwargs) -> None:
        calls["save"] += 1

    def session_factory():
        calls["session"] += 1
        raise AssertionError("group menu must not query accounts")

    class Message:
        from_user = SimpleNamespace(id=42, username="user", full_name="User")
        chat = SimpleNamespace(id=-100, type="group")

        def __init__(self) -> None:
            self.answers: list[str] = []

        async def answer(self, text: str, **kwargs) -> None:
            self.answers.append(text)

    monkeypatch.setattr(common, "save_user", save_user)
    monkeypatch.setattr(common, "AsyncSessionLocal", session_factory)

    async def scenario() -> None:
        message = Message()
        await common.cmd_start_help(message)
        assert calls == {"save": 0, "session": 0}
        assert message.answers
        assert "личном чате" in message.answers[0].lower()

    asyncio.run(scenario())


def test_main_menu_callbacks_reject_group_before_state_or_database_work(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import common

    def session_factory():
        raise AssertionError("group callback must not query accounts")

    class Message:
        chat = SimpleNamespace(type="group")

        def __init__(self) -> None:
            self.feedback: list[str] = []

        async def edit_text(self, *args, **kwargs) -> None:
            raise AssertionError("group callback must not render personal menu")

        async def answer(self, text, **kwargs) -> None:
            self.feedback.append(text)

    class Callback:
        from_user = SimpleNamespace(id=43, username="user", full_name="User")
        message = Message()

        def __init__(self) -> None:
            self.answers: list[tuple[tuple, dict]] = []

        async def answer(self, *args, **kwargs) -> None:
            self.answers.append((args, kwargs))

    class State:
        async def get_state(self):
            raise AssertionError("group callback must not touch personal state")

    monkeypatch.setattr(common, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(runtime, "AsyncSessionLocal", session_factory)

    async def scenario() -> None:
        for handler, args in (
            (common.callback_cancel_state_to_start, ()),
            (runtime.process_back_to_start, (State(),)),
        ):
            callback = Callback()
            await handler(callback, *args)
            assert len(callback.answers) == 1
            assert callback.answers[0] == ((), {})
            assert "личном чате" in callback.message.feedback[0]

    asyncio.run(scenario())


def test_back_to_start_acknowledges_before_edit_failure(monkeypatch) -> None:
    from guardbot.bot import runtime

    class Result:
        def scalars(self):
            return self

        def all(self):
            return []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def execute(self, statement):
            return Result()

    class Message:
        chat = SimpleNamespace(type="private")

    class Callback:
        from_user = SimpleNamespace(id=44, username="user", full_name="User")
        message = Message()

        def __init__(self) -> None:
            self.answer_calls = 0

        async def answer(self, *args, **kwargs) -> None:
            self.answer_calls += 1

    class State:
        async def get_state(self):
            return None

    async def fail_edit(*args, **kwargs):
        raise RuntimeError("edit failed")

    monkeypatch.setattr(runtime, "AsyncSessionLocal", Session)
    monkeypatch.setattr(runtime, "edit_main_menu", fail_edit)

    async def scenario() -> None:
        callback = Callback()
        with pytest.raises(RuntimeError, match="edit failed"):
            await runtime.process_back_to_start(callback, State())
        assert callback.answer_calls == 1

    asyncio.run(scenario())


def test_lock_middleware_rejects_every_group_callback_before_dispatch() -> None:
    from guardbot.bot import runtime

    handled = False

    class Callback:
        from_user = SimpleNamespace(id=45)
        data = "my_accounts"
        message = SimpleNamespace(chat=SimpleNamespace(type="group"))

        def __init__(self) -> None:
            self.answers = 0

        async def answer(self, *args, **kwargs) -> None:
            self.answers += 1

    async def handler(event, data):
        nonlocal handled
        handled = True

    async def scenario() -> None:
        callback = Callback()
        event = SimpleNamespace(message=None, callback_query=callback, inline_query=None)
        await runtime.LockCheckMiddleware()(handler, event, {})
        assert callback.answers == 1
        assert handled is False

    asyncio.run(scenario())
