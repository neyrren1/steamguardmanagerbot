import asyncio
import ast
import inspect
from pathlib import Path
import re
from types import SimpleNamespace

import pytest


class FakeState:
    def __init__(self) -> None:
        self.clear_calls = 0
        self.raw_state = None
        self.data: dict = {}

    async def clear(self) -> None:
        self.clear_calls += 1
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


class FakeMessage:
    def __init__(self, user_id: int = 781) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=user_id, type="private")
        self.message_thread_id = None
        self.business_connection_id = None
        self.answers: list[str] = []
        self.bot = SimpleNamespace(send_rich_message=self._send_rich_message)

    async def _send_rich_message(self, **_kwargs) -> None:
        return None

    async def answer(self, text: str, **_kwargs) -> None:
        self.answers.append(text)


@pytest.mark.parametrize("command_name", ["commands", "rich_demo"])
def test_local_navigation_commands_clear_pending_challenge(command_name: str) -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers.common import cmd_commands
    from guardbot.bot.handlers.rich_demo import cmd_rich_demo

    user_id = 781
    message = FakeMessage(user_id)
    state = FakeState()
    client = _ChallengeClient()

    async def scenario() -> None:
        await _publish_challenge(user_id, state, client)
        try:
            if command_name == "commands":
                await cmd_commands(message, state)
            else:
                await cmd_rich_demo(message, state)
            assert state.clear_calls == 1
            assert client.close_calls == 1
            assert pending_challenges.current_pending_challenge_token(user_id) is None
            assert await state.get_state() is None
            assert await state.get_data() == {}
        finally:
            await pending_challenges.clear_pending_challenges_for_user(user_id, state)

    asyncio.run(scenario())


class FakeCallbackMessage:
    def __init__(self) -> None:
        self.feedback: list[str] = []

    async def answer(self, text: str, **_kwargs) -> None:
        self.feedback.append(text)


class FakeCallback:
    def __init__(self, data: str) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=781)
        self.message = FakeCallbackMessage()
        self.answer_calls = 0

    async def answer(self, *_args, **_kwargs) -> None:
        self.answer_calls += 1


@pytest.mark.parametrize(
    ("handler_path", "handler_name", "callback_data", "needs_state"),
    [
        ("accounts", "callback_accounts_page", "accounts_page_not-an-int", False),
        ("accounts", "callback_accounts_page", "accounts_page_1_extra", False),
        ("groups", "callback_view_group", "view_group_not-an-int_0", False),
        ("groups", "callback_view_group", "view_group_1", False),
        ("inventory", "callback_inventory_app", "inventory_app_not-an-int_730_2_1", False),
        ("inventory", "callback_inventory_app", "inventory_app_1_730_2_1_extra_tail", False),
        ("inventory", "callback_inventory_app", "inventory_app_1_753_2_1", False),
        ("trade_notifications", "callback_trade_notifications_menu", "trade_notifications_menu_2147483648", True),
        ("trade_notifications", "callback_trade_notify_toggle", "trade_notify_toggle_²_off", True),
        ("trade_notifications", "callback_trade_notify_interval", "trade_notify_interval_2147483648", True),
        ("inventory", "callback_inventory_menu", "inventory_menu_2147483648", True),
        ("inventory", "callback_item_detail", "item_detail_1_2147483648_1", False),
        ("confirmations", "callback_confirmations_menu", "confirmations_menu_2147483648", False),
        ("confirmations", "callback_confirm_single", "confirm_single_1_not-an-int", False),
        ("confirmations", "callback_cancel_single", "cancel_single_1_18446744073709551616", False),
        ("confirmations", "callback_confirm_all", "confirm_all_2147483648", False),
        ("confirmations", "callback_cancel_all", "cancel_all_0", False),
        ("trade_operations", "callback_trade_accept", "trade_accept_1_2_18446744073709551616", False),
        ("trade_operations", "callback_trade_accept", "trade_accept_1_2_None", False),
        ("trade_operations", "callback_trade_accept", "trade_accept_1_2_https://steamcommunity.com/id/vanity", False),
        ("trade_operations", "callback_trade_decline", "trade_decline_1_18446744073709551616", False),
        ("trade_operations", "callback_trade_cancel_outgoing", "trade_cancel_1_not-an-int", False),
        ("trade_operations", "callback_trades_incoming_active", "trades_incoming_active_1_2147483648", False),
        ("trade_operations", "callback_trades_incoming_history", "trades_incoming_history_1_0", False),
        ("trade_operations", "callback_trades_outgoing_active", "trades_outgoing_active_2147483648_1", False),
        ("trade_operations", "callback_trades_outgoing_history", "trades_outgoing_history_1_1_extra", False),
        ("trade_operations", "callback_trade_detail_full", "trade_detail_full_1_2_sideways", False),
        ("trade_operations", "callback_back_to_trades_list", "back_to_trades_list_1_sideways", False),
    ],
)
def test_malformed_callbacks_are_rejected_before_database(
    monkeypatch,
    handler_path: str,
    handler_name: str,
    callback_data: str,
    needs_state: bool,
) -> None:
    from guardbot.bot.handlers import (
        accounts,
        confirmations,
        groups,
        inventory,
        trade_notifications,
        trade_operations,
    )

    module = {
        "accounts": accounts,
        "confirmations": confirmations,
        "groups": groups,
        "inventory": inventory,
        "trade_notifications": trade_notifications,
        "trade_operations": trade_operations,
    }[handler_path]
    handler = getattr(module, handler_name)

    def database_must_not_open():
        raise AssertionError("database must not be opened for malformed callback data")

    monkeypatch.setattr(module, "AsyncSessionLocal", database_must_not_open)
    callback = FakeCallback(callback_data)

    if needs_state:
        asyncio.run(handler(callback, FakeState()))
    else:
        asyncio.run(handler(callback))

    assert callback.answer_calls == 1
    assert callback.message.feedback == ["❌ Некорректное действие"]


def test_all_legacy_startswith_callbacks_validate_split_and_int_payloads() -> None:
    guardbot_dir = Path(__file__).resolve().parents[1] / "guardbot"
    unsafe: list[str] = []

    for path in guardbot_dir.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = " ".join(ast.unparse(item) for item in node.decorator_list)
            if "callback_query" not in decorators or ".startswith(" not in decorators:
                continue
            body = ast.get_source_segment(source, node) or ""
            parses_tokens = ".split(" in body or "int(" in body
            validates_tokens = (
                "validated_callback_data(" in decorators
                or "_parse_" in body
            )
            if parses_tokens and not validates_tokens:
                unsafe.append(f"{path.name}:{node.lineno}:{node.name}")

    assert unsafe == []


def test_callback_validator_patterns_name_every_variable_numeric_capture() -> None:
    guardbot_dir = Path(__file__).resolve().parents[1] / "guardbot"
    unsafe: list[str] = []

    for path in guardbot_dir.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                if not ast.unparse(decorator.func).endswith("validated_callback_data"):
                    continue
                if not decorator.args:
                    continue
                pattern = ast.literal_eval(decorator.args[0])
                if re.search(r"(?<!\\)\((?!\?)", pattern):
                    unsafe.append(f"{path.name}:{node.lineno}:anonymous")
                for match in re.finditer(r"\(\?P<([^>]+)>([^)]*)\)", pattern):
                    name, body = match.groups()
                    if "[0-9]" in body and not name.startswith(
                        ("db_id", "int32", "uint64")
                    ):
                        unsafe.append(f"{path.name}:{node.lineno}:{name}")

    assert unsafe == []


@pytest.mark.parametrize(
    "callback_data",
    [
        "sell_item_min_2147483648_730_1",
        "sell_item_min_1_²_1",
        "sell_item_min_1_730_18446744073709551616",
    ],
)
def test_sell_item_rejects_invalid_numeric_payload_before_proxy_check(
    monkeypatch, callback_data: str
) -> None:
    from guardbot.bot import runtime

    async def clear(*_args, **_kwargs):
        return None

    async def proxy_must_not_be_checked(*_args, **_kwargs):
        raise AssertionError("proxy lookup must not run for malformed callback data")

    monkeypatch.setattr(runtime, "clear_user_workflows", clear)
    monkeypatch.setattr(runtime, "check_proxy_available", proxy_must_not_be_checked)
    callback = FakeCallback(callback_data)

    asyncio.run(runtime.callback_sell_item(callback, FakeState()))

    assert callback.message.feedback == ["❌ Некорректное действие"]


def test_generated_group_filter_callbacks_reach_account_listing(monkeypatch) -> None:
    from guardbot.bot.handlers import groups

    calls: list[tuple[int, int | None]] = []

    async def show(_callback, page: int, group_id: int | None):
        calls.append((page, group_id))

    monkeypatch.setattr(groups, "show_accounts_page", show)

    async def scenario() -> None:
        await groups.callback_list_group_filter(FakeCallback("list_group_none_0"))
        await groups.callback_list_group_filter(FakeCallback("list_group_all_0"))

    asyncio.run(scenario())

    assert calls == [(0, 0), (0, None)]


def test_compact_callback_builders_stay_within_telegram_limit() -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import features

    payloads = [
        features._item_detail_callback(
            2_147_483_647,
            2_147_483_647,
            "18446744073709551615",
        ),
        features._confirmation_callback(
            "confirm",
            2_147_483_647,
            "18446744073709551615",
        ),
        features._confirmation_callback(
            "cancel",
            2_147_483_647,
            "18446744073709551615",
        ),
        features._trade_accept_callback(
            2_147_483_647,
            "18446744073709551615",
        ),
        features._trade_detail_callback(
            2_147_483_647,
            "18446744073709551615",
            "incoming",
        ),
        features._trade_decline_callback(
            2_147_483_647,
            "18446744073709551615",
        ),
        features._trade_cancel_callback(
            2_147_483_647,
            "18446744073709551615",
        ),
        features._inventory_page_callback(
            2_147_483_647,
            753,
            6,
            2_147_483_647,
            "18446744073709551615",
        ),
        runtime._trade_notification_callback(
            "detail",
            2_147_483_647,
            "18446744073709551615",
        ),
        runtime._trade_notification_callback(
            "decline",
            2_147_483_647,
            "18446744073709551615",
        ),
    ]

    assert all(payload is not None for payload in payloads)
    assert all(len(payload.encode("utf-8")) <= 64 for payload in payloads)
    assert runtime._trade_accept_callback(
        2_147_483_647,
        "18446744073709551615",
    ) == features._trade_accept_callback(
        2_147_483_647,
        "18446744073709551615",
    )


@pytest.mark.parametrize(
    ("builder_name", "args"),
    [
        ("_item_detail_callback", (1, 2_147_483_648, "1")),
        ("_item_detail_callback", (1, 730, "18446744073709551616")),
        ("_confirmation_callback", ("confirm", 1, "not-numeric")),
        ("_trade_accept_callback", (1, "18446744073709551616")),
        ("_trade_detail_callback", (1, "9" * 100, "incoming")),
        ("_trade_decline_callback", (1, "9" * 100)),
        ("_trade_cancel_callback", (1, "9" * 100)),
        ("_inventory_page_callback", (1, 753, 2, 1, "1")),
        ("_inventory_page_callback", (1, 753, 6, 1, "9" * 100)),
    ],
)
def test_compact_callback_builders_reject_invalid_identifiers(
    builder_name: str,
    args: tuple,
) -> None:
    from guardbot.bot.handlers import features

    assert getattr(features, builder_name)(*args) is None


def test_notification_callback_builder_rejects_invalid_trade_identifier() -> None:
    from guardbot.bot import runtime

    assert runtime._trade_notification_callback("detail", 1, "9" * 100) is None
    assert runtime._trade_notification_callback("decline", 1, "²") is None


def test_trade_navigation_and_html_paths_are_hardened() -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import bulk_import, inline, trade_operations

    trade_source = Path(inspect.getsourcefile(trade_operations)).read_text(encoding="utf-8")
    runtime_source = Path(inspect.getsourcefile(runtime)).read_text(encoding="utf-8")
    bulk_source = Path(inspect.getsourcefile(bulk_import)).read_text(encoding="utf-8")
    inline_source = Path(inspect.getsourcefile(inline)).read_text(encoding="utf-8")

    assert "trades_outgoing_{mafile_id}" not in trade_source
    assert "escape(str(e)[:" in trade_source
    assert "escape(str(mafile.account_name))" in runtime_source
    assert "escape(partner_name)" in runtime_source
    assert "<code>{escape(mf.account_name)}</code>" in bulk_source
    assert "<code>{escape(mafile.account_name)}</code>" in inline_source
    assert "<code>{escape(mf.account_name)}</code>" in inline_source


@pytest.mark.parametrize(
    ("handler_name", "callback_data", "allow"),
    [
        ("callback_confirm_single", "confirm_single_1_2", True),
        ("callback_cancel_single", "cancel_single_1_2", False),
    ],
)
def test_single_confirmation_callbacks_refresh_nonce_before_action(
    monkeypatch,
    handler_name: str,
    callback_data: str,
    allow: bool,
) -> None:
    from guardbot.bot.handlers import confirmations

    sent = []

    class Client:
        async def get_trade_confirmations(self):
            return {"success": True, "conf": [{"id": "2", "nonce": "fresh"}]}

        async def send_confirmation(self, conf_id, nonce, *, allow):
            sent.append((conf_id, nonce, allow))
            return True

        async def close(self):
            return None

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def owned(*_args, **_kwargs):
        return SimpleNamespace(id=1)

    async def create(*_args, **_kwargs):
        return Client()

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(confirmations, "AsyncSessionLocal", Session)
    monkeypatch.setattr(confirmations, "get_owned_mafile", owned)
    monkeypatch.setattr(confirmations.SteamSessionManager, "create_steam_client_from_db", create)
    monkeypatch.setattr(confirmations.SteamSessionManager, "save_steam_session_to_db", no_op)
    monkeypatch.setattr(confirmations, "callback_confirmations_menu", no_op)
    monkeypatch.setattr(confirmations, "callback_feedback", no_op)

    asyncio.run(getattr(confirmations, handler_name)(FakeCallback(callback_data)))

    assert sent == [("2", "fresh", allow)]


def test_trade_list_escapes_hostile_steam_fields() -> None:
    from guardbot.bot.handlers import trade_operations

    class Message:
        def __init__(self) -> None:
            self.edits = []

        async def edit_text(self, text, **kwargs):
            self.edits.append((text, kwargs))
            return self

    class Client:
        async def get_trade_offers_page(self, **_kwargs):
            return "html"

        async def parse_trade_offers_list(self, _html):
            return [{
                "status": "active",
                "tradeofferid": "123",
                "partner_id": "https://steamcommunity.com/id/vanity",
                "partner_name": "</a><b>spoof</b>",
                "message": "</i><a href='https://evil'>click</a>",
                "your_items_count": 1,
                "their_items_count": 2,
                "status_text": "<b>bad</b>",
                "accepted_date": "<i>date</i>",
                "protected_until": "<u>hold</u>",
                "expires_text": "<s>soon</s>",
            }]

    message = Message()
    callback = SimpleNamespace(message=message)

    asyncio.run(
        trade_operations.fetch_and_display_trades(
            callback,
            1,
            Client(),
            "incoming_active",
            1,
        )
    )

    text, kwargs = message.edits[-1]
    assert kwargs["parse_mode"] == "HTML"
    assert "</a><b>spoof</b>" not in text
    assert "&lt;/a&gt;&lt;b&gt;spoof&lt;/b&gt;" in text
    assert "https://evil" in text
    assert "href=&#x27;https://evil&#x27;" in text
    assert "<b>bad</b>" not in text
    assert "&lt;b&gt;bad&lt;/b&gt;" in text


def test_trade_list_omits_oversized_external_identifier_callback() -> None:
    from guardbot.bot.handlers import trade_operations

    class Message:
        def __init__(self) -> None:
            self.edits = []

        async def edit_text(self, text, **kwargs):
            self.edits.append((text, kwargs))
            return self

    class Client:
        async def get_trade_offers_page(self, **_kwargs):
            return "html"

        async def parse_trade_offers_list(self, _html):
            return [{
                "status": "active",
                "tradeofferid": "9" * 100,
                "partner_id": "1",
                "partner_name": "partner",
                "message": "",
                "your_items_count": 0,
                "their_items_count": 0,
                "status_text": "active",
                "accepted_date": None,
                "protected_until": None,
                "expires_text": None,
            }]

    message = Message()
    callback = SimpleNamespace(message=message)

    asyncio.run(
        trade_operations.fetch_and_display_trades(
            callback,
            1,
            Client(),
            "incoming_active",
            1,
        )
    )

    keyboard = message.edits[-1][1]["reply_markup"]
    payloads = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert all(len(payload.encode("utf-8")) <= 64 for payload in payloads)
    assert not any(("9" * 100) in payload for payload in payloads)


def test_html_rendering_escapes_direct_untrusted_values() -> None:
    guardbot_dir = Path(__file__).resolve().parents[1] / "guardbot" / "bot"
    unsafe: set[str] = set()
    tainted_fragments = (
        ".account_name",
        "account_name",
        "account_input",
        "mafile_data",
        "str(e)",
        "redact_proxy_url",
        "external_ip",
        "profile_url",
    )

    for path in guardbot_dir.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
            html_mode = any(
                keyword.arg == "parse_mode"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value == "HTML"
                for keyword in call.keywords
            )
            if not html_mode:
                continue
            rendered_values = [
                *call.args,
                *(
                    keyword.value
                    for keyword in call.keywords
                    if keyword.arg in {"text", "caption", "message_text"}
                ),
            ]
            for rendered_value in rendered_values:
                if isinstance(rendered_value, ast.Call):
                    continue
                for value in (
                    node
                    for node in ast.walk(rendered_value)
                    if isinstance(node, ast.FormattedValue)
                ):
                    expression = ast.unparse(value.value)
                    if (
                        any(fragment in expression for fragment in tainted_fragments)
                        and not expression.startswith("escape(")
                        and not expression.startswith("safe_")
                    ):
                        unsafe.add(
                            f"{path.relative_to(guardbot_dir)}:"
                            f"{value.lineno}:{expression}"
                        )

        for joined in (node for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)):
            literal_text = "".join(
                str(value.value)
                for value in joined.values
                if isinstance(value, ast.Constant)
            )
            if "<" not in literal_text:
                continue
            for value in (
                node
                for node in ast.walk(joined)
                if isinstance(node, ast.FormattedValue)
            ):
                expression = ast.unparse(value.value)
                if (
                    any(fragment in expression for fragment in tainted_fragments)
                    and not expression.startswith("escape(")
                    and not expression.startswith("safe_")
                ):
                    unsafe.add(
                        f"{path.relative_to(guardbot_dir)}:"
                        f"{value.lineno}:{expression}"
                    )

    assert sorted(unsafe) == []
