import ast
import asyncio
import base64
import inspect
from pathlib import Path

import pytest

import guardbot
import tgbot
from guardbot import steam
from guardbot.steam import client as steam_client
from guardbot.steam.mixins import (
    ConfirmationsMixin,
    InventoryMixin,
    MarketMixin,
    ProfileMixin,
    SessionRecoveryMixin,
    TradeOffersMixin,
    TradeRenderingMixin,
)

ROOT = Path(__file__).resolve().parents[1]

FEATURE_MODULES = {
    "guardbot/steam/mixins/__init__.py",
    "guardbot/steam/mixins/recovery.py",
    "guardbot/steam/mixins/profile.py",
    "guardbot/steam/mixins/inventory.py",
    "guardbot/steam/mixins/confirmations.py",
    "guardbot/steam/mixins/market.py",
    "guardbot/steam/mixins/trades.py",
    "guardbot/steam/mixins/rendering.py",
}

LEGACY_CLIENT_EXPORTS = {
    "AlreadyHasAPhoneNumber",
    "AlreadyHasMobileSteamGuard",
    "AsyncSteamMobile",
    "AsyncSteamSession",
    "EmailNotVerified",
    "InvalidCredentials",
    "InvalidSMSCode",
    "InvalidSteamGuardCode",
    "LoginConfirmType",
    "NoAccountName",
    "ProxyRequiredError",
    "RefreshTokenEmpty",
    "RefreshTokenExpired",
    "UnableToGenerateCorrectCodes",
    "UnknownException",
    "addAuthenticator",
    "beginAuthSessionViaCredentials",
    "finalizelogin",
    "finalizeAddAuthenticator",
    "generate_device_id",
    "getPasswordRSAPublicKey",
    "parse_proxy_string",
    "pollAuthSessionStatus",
    "probe_proxy_external_ip",
    "redact_proxy_url",
    "resolve_public_proxy_url",
    "updateAuthSessionWithSteamGuardCode",
    "validate_steam_asset_url",
}

SYNC_METHODS = {
    "generate_steam_guard_code",
    "generate_steam_guard_code_for_time",
    "load_mobile",
}

ASYNC_METHODS = {
    "_download_steam_asset",
    "_ensure_session",
    "_ensure_sessionid",
    "_request",
    "accept_trade_offer",
    "cancel_trade_offer",
    "check_session_valid",
    "confirm_login",
    "confirm_market_listing",
    "decline_trade_offer",
    "ensure_valid_session",
    "generate_trade_image",
    "get_account_country",
    "get_inventory",
    "get_inventory_paginated",
    "get_trade_confirmations",
    "get_trade_offer_details",
    "get_trade_offers_page",
    "get_tradeable_inventory",
    "get_tradelink",
    "login",
    "sell_item",
    "sell_item_with_recovery",
    "send_confirmation",
}

MIXIN_METHODS = {
    SessionRecoveryMixin: {
        "_full_session_recovery",
        "_try_recover_session",
        "check_session_valid",
        "ensure_valid_session",
    },
    ProfileMixin: {"get_account_country", "get_tradelink"},
    InventoryMixin: {
        "get_inventory",
        "get_inventory_paginated",
        "get_tradeable_inventory",
    },
    ConfirmationsMixin: {
        "_create_confirmation_params",
        "_generate_confirmation_key",
        "confirm_market_listing",
        "get_trade_confirmations",
        "send_confirmation",
    },
    MarketMixin: {"sell_item", "sell_item_with_recovery"},
    TradeOffersMixin: {
        "accept_trade_offer",
        "cancel_trade_offer",
        "decline_trade_offer",
        "get_trade_offer_details",
        "get_trade_offers_page",
        "parse_trade_offer_page_full",
        "parse_trade_offers_list",
    },
    TradeRenderingMixin: {"_download_steam_asset", "generate_trade_image"},
}

MIXIN_PARAMETERS = {
    "_create_confirmation_params": (("self", "tag_string"), {}),
    "_download_steam_asset": (("self", "asset_url"), {}),
    "_full_session_recovery": (("self",), {}),
    "_generate_confirmation_key": (
        ("self", "identity_secret", "tag", "timestamp"),
        {},
    ),
    "_try_recover_session": (("self",), {}),
    "accept_trade_offer": (("self", "tradeofferid", "partner_id"), {"partner_id": None}),
    "cancel_trade_offer": (("self", "tradeofferid"), {}),
    "check_session_valid": (("self",), {}),
    "confirm_market_listing": (("self", "conf_id", "allow"), {"allow": True}),
    "decline_trade_offer": (("self", "tradeofferid"), {}),
    "ensure_valid_session": (("self",), {}),
    "generate_trade_image": (("self", "trade_info"), {}),
    "get_account_country": (("self",), {}),
    "get_inventory": (
        ("self", "app_id", "context_id"),
        {"app_id": 730, "context_id": 2},
    ),
    "get_inventory_paginated": (
        ("self", "app_id", "context_id", "start_assetid", "count"),
        {"app_id": 730, "context_id": 2, "start_assetid": None, "count": 15},
    ),
    "get_trade_confirmations": (("self",), {}),
    "get_trade_offer_details": (("self", "tradeofferid"), {}),
    "get_trade_offers_page": (
        ("self", "get_received", "active_only", "history"),
        {"get_received": True, "active_only": True, "history": False},
    ),
    "get_tradeable_inventory": (("self",), {}),
    "get_tradelink": (("self",), {}),
    "parse_trade_offer_page_full": (("self", "tradeofferid"), {}),
    "parse_trade_offers_list": (("self", "html_text"), {}),
    "sell_item": (
        ("self", "app_id", "context_id", "assetid", "amount", "price"),
        {},
    ),
    "sell_item_with_recovery": (
        ("self", "app_id", "context_id", "assetid", "amount", "price"),
        {},
    ),
    "send_confirmation": (
        ("self", "conf_id", "conf_key", "allow"),
        {"allow": True},
    ),
}


def test_steam_client_is_split_into_feature_modules() -> None:
    missing = sorted(path for path in FEATURE_MODULES if not (ROOT / path).is_file())
    assert not missing, f"Missing Steam feature modules: {missing}"


def test_official_steam_reexports_keep_identity() -> None:
    assert guardbot.__all__ == ["AsyncSteamMobile", "parse_proxy_string"]
    assert steam.__all__ == ["AsyncSteamMobile", "parse_proxy_string"]
    assert guardbot.AsyncSteamMobile is steam_client.AsyncSteamMobile
    assert steam.AsyncSteamMobile is steam_client.AsyncSteamMobile
    assert guardbot.parse_proxy_string is steam_client.parse_proxy_string
    assert steam.parse_proxy_string is steam_client.parse_proxy_string


@pytest.mark.parametrize("name", sorted(LEGACY_CLIENT_EXPORTS))
def test_legacy_steam_exports_keep_identity(name: str) -> None:
    assert hasattr(steam_client, name)
    assert getattr(tgbot, name) is getattr(steam_client, name)


def test_mobile_client_retains_session_contract() -> None:
    client_type = steam_client.AsyncSteamMobile
    assert issubclass(client_type, steam_client.AsyncSteamSession)
    assert client_type.__mro__.count(steam_client.AsyncSteamSession) == 1

    for method_name in SYNC_METHODS:
        method = getattr(client_type, method_name)
        assert not inspect.iscoroutinefunction(method), method_name

    for method_name in ASYNC_METHODS:
        method = getattr(client_type, method_name)
        assert inspect.iscoroutinefunction(method), method_name


def test_feature_methods_have_explicit_non_overlapping_owners() -> None:
    session_names = set(steam_client.AsyncSteamSession.__dict__)
    seen: set[str] = set()

    for mixin, expected_methods in MIXIN_METHODS.items():
        actual_methods = {
            name
            for name, value in mixin.__dict__.items()
            if inspect.isfunction(value)
        }
        assert actual_methods == expected_methods
        assert not session_names.intersection(actual_methods)
        assert not seen.intersection(actual_methods)
        seen.update(actual_methods)

    assert len(seen) == 25
    assert seen == set(MIXIN_PARAMETERS)


def test_feature_method_signatures_and_async_contract_are_preserved() -> None:
    sync_methods = {"_create_confirmation_params", "_generate_confirmation_key"}

    for method_name, (parameter_names, expected_defaults) in MIXIN_PARAMETERS.items():
        method = getattr(steam_client.AsyncSteamMobile, method_name)
        signature = inspect.signature(method)
        actual_defaults = {
            name: parameter.default
            for name, parameter in signature.parameters.items()
            if parameter.default is not inspect.Parameter.empty
        }

        assert tuple(signature.parameters) == parameter_names
        assert actual_defaults == expected_defaults
        assert inspect.iscoroutinefunction(method) is (method_name not in sync_methods)


def test_asset_validator_remains_patchable_through_client_facade(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    def fake_validate(asset_url: str) -> str:
        calls.append(("validate", asset_url))
        return "https://avatars.cloudflare.steamstatic.com/patched.png"

    class FakeResponse:
        status = 200

        def __init__(self) -> None:
            self.headers = {"Content-Type": "image/png"}

        async def read(self) -> bytes:
            return b"image"

    async def fake_request(method: str, url: str, **kwargs):
        calls.append((method, url))
        return FakeResponse()

    monkeypatch.setattr(steam_client, "validate_steam_asset_url", fake_validate)
    client = steam_client.AsyncSteamMobile("account", "password")
    monkeypatch.setattr(client, "_request", fake_request)

    assert asyncio.run(client._download_steam_asset("facade-patch-point")) == b"image"
    assert calls == [
        ("validate", "facade-patch-point"),
        ("GET", "https://avatars.cloudflare.steamstatic.com/patched.png"),
    ]


def test_guard_code_remains_fully_offline_without_proxy(monkeypatch) -> None:
    def unexpected_client_session(*args, **kwargs):
        raise AssertionError("offline Guard code must not create an HTTP session")

    monkeypatch.setattr(steam_client.aiohttp, "ClientSession", unexpected_client_session)
    monkeypatch.setattr(steam_client.time, "time", lambda: 1_700_000_000)

    client = steam_client.AsyncSteamMobile("account", "password")
    client.shared_secret = base64.b64encode(b"01234567890123456789").decode()

    assert client.generate_steam_guard_code_for_time(1_700_000_000) == "N3FRN"
    assert client.generate_steam_guard_code() == "N3FRN"
    assert client.session is None
    assert client._cookie_jar is None


def test_feature_mixins_do_not_import_compatibility_facade() -> None:
    for relative_path in FEATURE_MODULES:
        if relative_path.endswith("__init__.py"):
            continue
        path = ROOT / relative_path
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports_facade = any(
            (
                isinstance(node, ast.ImportFrom)
                and node.module == "guardbot.steam.client"
            )
            or (
                isinstance(node, ast.Import)
                and any(
                    alias.name == "guardbot.steam.client" for alias in node.names
                )
            )
            for node in ast.walk(tree)
        )
        assert not imports_facade, relative_path
