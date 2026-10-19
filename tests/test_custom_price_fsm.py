import asyncio
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


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _StatusMessage:
    def __init__(self) -> None:
        self.edits = []

    async def edit_text(self, text: str, **kwargs) -> None:
        self.edits.append((text, kwargs))


class _InputMessage:
    def __init__(self, state: FSMContext, text: str, chat_type: str = "private") -> None:
        self.state = state
        self.text = text
        self.chat = SimpleNamespace(type=chat_type)
        self.from_user = SimpleNamespace(id=USER_ID)
        self.answers = []

    async def answer(self, text: str, **kwargs):
        status = _StatusMessage()
        self.answers.append((text, kwargs, await self.state.get_state(), status))
        return status


class _PromptMessage:
    def __init__(self, state: FSMContext, error: BaseException | None = None) -> None:
        self.state = state
        self.error = error
        self.edits = []
        self.states_during_edit = []

    async def edit_text(self, text: str, **kwargs) -> None:
        self.states_during_edit.append(await self.state.get_state())
        self.edits.append((text, kwargs))
        if self.error is not None:
            raise self.error

    async def answer(self, *_args, **_kwargs) -> None:
        return None


class _Callback:
    def __init__(self, state: FSMContext, error: BaseException | None = None) -> None:
        self.from_user = SimpleNamespace(id=USER_ID)
        self.data = "sell_item_custom_42_730_51107204438"
        self.message = _PromptMessage(state, error)
        self._guardbot_acknowledged = True


def test_custom_sell_callback_starts_typed_flow_before_prompt(monkeypatch) -> None:
    from guardbot.bot import pending_challenges, runtime
    from guardbot.bot.states import CustomPriceFlow

    async def proxy_available(*_args, **_kwargs):
        return True, "proxy"

    async def owned(*_args, **_kwargs):
        return SimpleNamespace(id=42)

    async def feedback(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "check_proxy_available", proxy_available)
    monkeypatch.setattr(runtime, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(runtime, "get_owned_mafile", owned)
    monkeypatch.setattr(runtime, "callback_feedback", feedback)

    async def scenario() -> None:
        state = _state()
        client = _ChallengeClient()
        await _publish_challenge(USER_ID, state, client)
        await state.update_data(stale=True)
        callback = _Callback(state)

        try:
            await runtime.callback_sell_item(callback, state)

            assert client.close_calls == 1
            assert pending_challenges.current_pending_challenge_token(USER_ID) is None
            assert await state.get_state() == CustomPriceFlow.waiting_price.state
            assert await state.get_data() == {
                "mafile_id": 42,
                "app_id": 730,
                "assetid": "51107204438",
                "context_id": 2,
            }
            assert callback.message.states_during_edit == [
                CustomPriceFlow.waiting_price.state
            ]
        finally:
            await pending_challenges.clear_pending_challenges_for_user(USER_ID, state)

    asyncio.run(scenario())


def test_custom_sell_uses_steam_inventory_context_for_app_753(monkeypatch) -> None:
    from guardbot.bot import runtime

    async def proxy_available(*_args, **_kwargs):
        return True, "proxy"

    async def owned(*_args, **_kwargs):
        return SimpleNamespace(id=42)

    monkeypatch.setattr(runtime, "check_proxy_available", proxy_available)
    monkeypatch.setattr(runtime, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(runtime, "get_owned_mafile", owned)

    async def scenario() -> None:
        state = _state()
        callback = _Callback(state)
        callback.data = "sell_item_custom_42_753_51107204438"

        await runtime.callback_sell_item(callback, state)

        assert await state.get_data() == {
            "mafile_id": 42,
            "app_id": 753,
            "assetid": "51107204438",
            "context_id": 6,
        }

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [RuntimeError("failed"), asyncio.CancelledError()])
def test_custom_price_prompt_failure_rolls_back(monkeypatch, error: BaseException) -> None:
    from guardbot.bot import runtime

    async def proxy_available(*_args, **_kwargs):
        return True, "proxy"

    async def owned(*_args, **_kwargs):
        return SimpleNamespace(id=42)

    monkeypatch.setattr(runtime, "check_proxy_available", proxy_available)
    monkeypatch.setattr(runtime, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(runtime, "get_owned_mafile", owned)

    async def scenario() -> None:
        state = _state()
        callback = _Callback(state, error)

        with pytest.raises(type(error)):
            await runtime.callback_sell_item(callback, state)

        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "callback_data",
    [
        "sell_item_custom_42_730_51107204438_extra",
        "sell_item_custom_-42_730_51107204438",
        "sell_item_custom_42_730_bad_asset",
        "sell_item_unknown_42_730_51107204438",
    ],
)
def test_malformed_sell_callback_clears_before_proxy(
    monkeypatch,
    callback_data: str,
) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.states import CustomPriceFlow

    async def unexpected_proxy(*_args, **_kwargs):
        raise AssertionError("Malformed callback reached proxy resolution")

    monkeypatch.setattr(runtime, "check_proxy_available", unexpected_proxy)

    async def scenario() -> None:
        state = _state()
        await state.set_state(CustomPriceFlow.waiting_price)
        await state.set_data({"mafile_id": 99})
        callback = _Callback(state)
        callback.data = callback_data

        await runtime.callback_sell_item(callback, state)

        assert await state.get_state() is None
        assert await state.get_data() == {}

    asyncio.run(scenario())


@pytest.mark.parametrize(("app_id", "context_id"), [(730, 2), (753, 6)])
def test_custom_price_clears_before_steam_sale(
    monkeypatch,
    app_id: int,
    context_id: int,
) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.states import CustomPriceFlow

    state_seen_by_sale = []
    sale_args = []

    class Client:
        def __init__(self, state: FSMContext) -> None:
            self.state = state
            self.closed = 0

        async def sell_item_with_recovery(self, *args):
            state_seen_by_sale.append(await self.state.get_state())
            sale_args.append(args)
            return {"success": True, "needs_mobile_confirmation": False}

        async def close(self) -> None:
            self.closed += 1

    async def owned(*_args, **_kwargs):
        return SimpleNamespace(id=42, currency_id=18)

    monkeypatch.setattr(runtime, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(runtime, "get_owned_mafile", owned)
    monkeypatch.setattr(runtime, "calculate_steam_price", lambda *_args: 757)

    async def scenario() -> None:
        state = _state()
        await state.set_state(CustomPriceFlow.waiting_price)
        await state.set_data({
            "mafile_id": 42,
            "app_id": app_id,
            "assetid": "51107204438",
            "context_id": context_id,
        })
        client = Client(state)

        async def create_client(*_args, **_kwargs):
            return client

        monkeypatch.setattr(
            runtime.SteamSessionManager,
            "create_steam_client_from_db",
            create_client,
        )
        message = _InputMessage(state, "8.70")

        await runtime.process_custom_price(message, state)

        assert state_seen_by_sale == [None]
        assert sale_args == [(app_id, context_id, "51107204438", 1, 757)]
        assert await state.get_state() is None
        assert await state.get_data() == {}
        assert client.closed == 1
        assert "успешно выставлен" in message.answers[0][3].edits[0][0]

    asyncio.run(scenario())


def test_status_prompt_failure_keeps_retry_and_closes_client(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.states import CustomPriceFlow

    client_holder = {}

    class Client:
        def __init__(self, state: FSMContext) -> None:
            self.state = state
            self.closed = 0

        async def close(self) -> None:
            self.closed += 1

    class FailingMessage(_InputMessage):
        async def answer(self, *_args, **_kwargs):
            raise RuntimeError("Telegram unavailable")

    async def owned(*_args, **_kwargs):
        return SimpleNamespace(id=42, currency_id=18)

    async def create_client(*_args, **_kwargs):
        client = Client(client_holder["state"])
        client_holder["client"] = client
        return client

    monkeypatch.setattr(runtime, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(runtime, "get_owned_mafile", owned)
    monkeypatch.setattr(
        runtime.SteamSessionManager,
        "create_steam_client_from_db",
        create_client,
    )
    monkeypatch.setattr(runtime, "calculate_steam_price", lambda *_args: 757)

    async def scenario() -> None:
        state = _state()
        client_holder["state"] = state
        expected_data = {
            "mafile_id": 42,
            "app_id": 730,
            "assetid": "51107204438",
            "context_id": 2,
        }
        await state.set_state(CustomPriceFlow.waiting_price)
        await state.set_data(expected_data)

        with pytest.raises(RuntimeError, match="Telegram unavailable"):
            await runtime.process_custom_price(FailingMessage(state, "8.70"), state)

        assert await state.get_state() == CustomPriceFlow.waiting_price.state
        assert await state.get_data() == expected_data
        assert client_holder["client"].closed == 1

    asyncio.run(scenario())


def test_custom_price_no_longer_uses_legacy_dispatch() -> None:
    from guardbot.bot import runtime
    from guardbot.bot.handlers import messages

    sources = inspect.getsource(runtime) + inspect.getsource(messages)
    assert '"waiting_custom_price"' not in sources


@pytest.mark.parametrize(
    "text",
    [
        "abc",
        "0",
        "NaN",
        "Infinity",
        "1e9999",
        "1e308",
        "1e-1000000",
        "21474836.48",
        "8.701",
    ],
)
def test_invalid_custom_price_keeps_flow_for_retry(monkeypatch, text: str) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.states import CustomPriceFlow

    monkeypatch.setattr(
        runtime,
        "AsyncSessionLocal",
        lambda: pytest.fail("invalid price must not access the database"),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(CustomPriceFlow.waiting_price)
        expected_data = {
            "mafile_id": 42,
            "app_id": 730,
            "assetid": "51107204438",
            "context_id": 2,
        }
        await state.set_data(expected_data)
        message = _InputMessage(state, text)

        await runtime.process_custom_price(message, state)

        assert await state.get_state() == CustomPriceFlow.waiting_price.state
        assert await state.get_data() == expected_data
        assert len(message.answers) == 1

    asyncio.run(scenario())


def test_custom_price_from_group_preserves_private_flow(monkeypatch) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.states import CustomPriceFlow

    monkeypatch.setattr(
        runtime,
        "AsyncSessionLocal",
        lambda: pytest.fail("cross-chat input must not access the database"),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(CustomPriceFlow.waiting_price)
        expected_data = {
            "mafile_id": 42,
            "app_id": 730,
            "assetid": "51107204438",
            "context_id": 2,
        }
        await state.set_data(expected_data)
        message = _InputMessage(state, "8.70", chat_type="group")

        await runtime.process_custom_price(message, state)

        assert await state.get_state() == CustomPriceFlow.waiting_price.state
        assert await state.get_data() == expected_data
        assert "личном чате" in message.answers[0][0]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "state_data",
    [
        {"mafile_id": 42, "app_id": 730, "assetid": "51107204438"},
        {"mafile_id": True, "app_id": 730, "assetid": "51107204438", "context_id": 2},
        {"mafile_id": 42, "app_id": False, "assetid": "51107204438", "context_id": 2},
        {"mafile_id": 42, "app_id": 730, "assetid": "511_072", "context_id": 2},
        {"mafile_id": 42, "app_id": 730, "assetid": "51107204438", "context_id": True},
    ],
)
def test_malformed_custom_price_state_clears_before_database(
    monkeypatch,
    state_data: dict,
) -> None:
    from guardbot.bot import runtime
    from guardbot.bot.states import CustomPriceFlow

    monkeypatch.setattr(
        runtime,
        "AsyncSessionLocal",
        lambda: pytest.fail("malformed FSM data must not access the database"),
    )

    async def scenario() -> None:
        state = _state()
        await state.set_state(CustomPriceFlow.waiting_price)
        await state.set_data(state_data)
        message = _InputMessage(state, "8.70")

        await runtime.process_custom_price(message, state)

        assert await state.get_state() is None
        assert await state.get_data() == {}
        assert len(message.answers) == 1

    asyncio.run(scenario())


def test_inventory_cancel_clears_custom_price_before_proxy_check(monkeypatch) -> None:
    from guardbot.bot.handlers import inventory
    from guardbot.bot.states import CustomPriceFlow

    holder = {}
    states_during_proxy_check = []

    async def unavailable_proxy(*_args, **_kwargs):
        states_during_proxy_check.append(await holder["state"].get_state())
        return False, None

    async def feedback(*_args, **_kwargs):
        return None

    monkeypatch.setattr(inventory, "check_proxy_available", unavailable_proxy)
    monkeypatch.setattr(inventory, "callback_feedback", feedback)

    async def scenario() -> None:
        state = _state()
        holder["state"] = state
        await state.set_state(CustomPriceFlow.waiting_price)
        await state.set_data({
            "mafile_id": 42,
            "app_id": 730,
            "assetid": "51107204438",
            "context_id": 2,
        })
        callback = _Callback(state)
        callback.data = "inventory_menu_42"

        await inventory.callback_inventory_menu(callback, state)

        assert await state.get_state() is None
        assert await state.get_data() == {}
        assert states_during_proxy_check == [None]

    asyncio.run(scenario())


def test_cancel_to_start_clears_custom_price_before_database(monkeypatch) -> None:
    from guardbot.bot.handlers import common
    from guardbot.bot.states import CustomPriceFlow

    holder = {}

    class Session:
        async def __aenter__(self):
            assert await holder["state"].get_state() is None
            assert await holder["state"].get_data() == {}
            raise RuntimeError("stop before database")

        async def __aexit__(self, *_args):
            return False

    class Message:
        chat = SimpleNamespace(type="private")

        async def answer(self, *_args, **_kwargs):
            return None

    class Callback:
        from_user = SimpleNamespace(
            id=USER_ID,
            username="tester",
            full_name="Tester",
        )
        message = Message()
        _guardbot_acknowledged = True

    monkeypatch.setattr(common, "AsyncSessionLocal", Session)

    async def scenario() -> None:
        state = _state()
        holder["state"] = state
        await state.set_state(CustomPriceFlow.waiting_price)
        await state.set_data({
            "mafile_id": 42,
            "app_id": 730,
            "assetid": "51107204438",
            "context_id": 2,
        })

        try:
            with pytest.raises(RuntimeError, match="stop before database"):
                await common.callback_cancel_state_to_start(Callback(), state)

            assert await state.get_state() is None
            assert await state.get_data() == {}
        finally:
            await state.clear()

    asyncio.run(scenario())


def test_item_detail_escapes_untrusted_steam_html(monkeypatch) -> None:
    from guardbot.bot.handlers import inventory

    hostile = '</code><a href="tg://user?id=1">x</a><code>'
    rendered = []

    class Client:
        closed = 0
        inventory_calls = []

        async def get_inventory(self, *args):
            self.inventory_calls.append(args)
            return {"assets": [{}]}

        async def _ensure_session(self):
            return None

        async def close(self):
            self.closed += 1

    client = Client()

    item = {
        "assetid": "51107204438",
        "name": hostile,
        "name_tag": hostile,
        "item_type": hostile,
        "rarity_emoji": hostile,
        "rarity": hostile,
        "exterior": hostile,
        "collection": hostile,
        "hero": hostile,
        "quality": hostile,
        "float": 0.123,
        "pattern": hostile,
        "paint_seed": hostile,
        "is_stattrak": True,
        "stattrak_count": hostile,
        "tradable": True,
        "marketable": True,
        "amount": 2,
        "inspect_link": hostile,
        "stickers": [{"name": hostile, "wear": 0.1}],
        "market_hash_name": hostile,
    }

    async def owned(*_args, **_kwargs):
        return SimpleNamespace(
            id=42,
            account_name="account",
            country="US",
            currency_id=1,
        )

    async def create_client(*_args, **_kwargs):
        return client

    async def get_price(*_args, **_kwargs):
        return {
            "lowest_price": f"1.00 {hostile}",
            "median_price": f"2.00 {hostile}",
            "volume": hostile,
        }

    async def feedback(*_args, **_kwargs):
        return None

    class Message:
        async def edit_text(self, text, **kwargs):
            rendered.append((text, kwargs))

        async def answer(self, *_args, **_kwargs):
            return None

    callback = SimpleNamespace(
        data="item_detail_42_753_51107204438",
        from_user=SimpleNamespace(id=USER_ID),
        message=Message(),
        _guardbot_acknowledged=True,
    )

    monkeypatch.setattr(inventory, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(inventory, "get_owned_mafile", owned)
    monkeypatch.setattr(
        inventory.SteamSessionManager,
        "create_steam_client_from_db",
        create_client,
    )
    monkeypatch.setattr(
        inventory,
        "process_inventory_items_universal",
        lambda *_args: [item],
    )
    monkeypatch.setattr(inventory, "get_item_price", get_price)
    monkeypatch.setattr(inventory, "callback_feedback", feedback)

    asyncio.run(inventory.callback_item_detail(callback))

    assert len(rendered) == 1
    text, kwargs = rendered[0]
    assert hostile not in text
    assert "&lt;/code&gt;&lt;a href=&quot;" in text
    assert kwargs["parse_mode"] == "HTML"
    assert "%3C%2Fcode%3E" in kwargs["reply_markup"].inline_keyboard[-3][0].url
    assert (
        kwargs["reply_markup"].inline_keyboard[-2][0].callback_data
        == "inventory_app_42_753_6_1"
    )
    assert client.inventory_calls == [(753, 6)]
    assert client.closed == 1


def test_dispatcher_rejects_cross_chat_custom_price_input(monkeypatch) -> None:
    from datetime import UTC, datetime

    from aiogram import Bot
    from aiogram.methods import SendMessage
    from aiogram.types import Chat, Message, Update, User

    from guardbot.bot import runtime
    from guardbot.bot.states import CustomPriceFlow

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
            raise AssertionError(f"Unexpected Bot API method: {method!r}")

    session_calls = []

    def session_factory():
        session_calls.append(True)
        return LockSession()

    monkeypatch.setattr(runtime, "AsyncSessionLocal", session_factory)

    async def scenario() -> None:
        bot = FakeBot()
        state = runtime.dp.fsm.get_context(bot=bot, chat_id=USER_ID, user_id=USER_ID)
        try:
            await state.set_state(CustomPriceFlow.waiting_price)
            expected_data = {
                "mafile_id": 42,
                "app_id": 730,
                "assetid": "51107204438",
                "context_id": 2,
            }
            await state.set_data(expected_data)

            await runtime.dp.feed_update(
                bot,
                Update(
                    update_id=1,
                    message=Message(
                        message_id=1,
                        date=datetime.now(UTC),
                        chat=Chat(id=-100123, type="group"),
                        from_user=User(
                            id=USER_ID,
                            is_bot=False,
                            first_name="Test",
                        ),
                        text="8.70",
                    ),
                ),
            )

            assert await state.get_state() == CustomPriceFlow.waiting_price.state
            assert await state.get_data() == expected_data
            assert len(session_calls) == 1
            assert len(bot.calls) == 1
            assert "личном чате" in bot.calls[0].text
        finally:
            await state.clear()
            await bot.session.close()

    asyncio.run(scenario())
