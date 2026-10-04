import asyncio
import base64

import pytest

import tgbot


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", None),
        ("http://user:pass@example.test:8080", "http://user:pass@example.test:8080"),
        ("example.test:1080", "socks5://example.test:1080"),
        ("example.test:1081", "socks5://example.test:1081"),
        ("example.test:46281", "socks5://example.test:46281"),
        ("example.test:8080", "http://example.test:8080"),
        ("example.test:9000", "http://example.test:9000"),
    ],
)
def test_parse_proxy_string_preserves_existing_behavior(raw: str, expected: str | None) -> None:
    assert tgbot.parse_proxy_string(raw) == expected


def test_steam_guard_code_generation_is_deterministic() -> None:
    async def check_code() -> None:
        client = tgbot.AsyncSteamMobile("account", "password", proxy="http://proxy.test:8080")
        client.shared_secret = base64.b64encode(b"01234567890123456789").decode()

        assert client.generate_steam_guard_code_for_time(1_700_000_000) == "N3FRN"

    asyncio.run(check_code())


def test_steam_price_calculation_preserves_rounding_and_minimum_commission() -> None:
    assert tgbot.calculate_steam_price(100.0, 18) == 8695
    assert tgbot.calculate_steam_price(1.0, 5) == -57


def test_handler_registration_is_not_lost() -> None:
    assert len(tgbot.dp.message.handlers) == 74
    assert len(tgbot.dp.callback_query.handlers) == 74
    assert len(tgbot.dp.inline_query.handlers) == 1
