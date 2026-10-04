import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from guardbot.steam import client as steam_client

ROOT = Path(__file__).resolve().parents[1]


def test_network_session_requires_proxy_before_client_creation(monkeypatch) -> None:
    def unexpected_client_session(*args, **kwargs):
        raise AssertionError(
            "direct network session must not be created without a proxy"
        )

    monkeypatch.setattr(
        steam_client.aiohttp, "ClientSession", unexpected_client_session
    )
    client = steam_client.AsyncSteamMobile("account", "password")

    with pytest.raises(steam_client.ProxyRequiredError):
        asyncio.run(client._ensure_session())


def test_time_alignment_without_proxy_is_strictly_offline(monkeypatch) -> None:
    calls = 0
    client = steam_client.AsyncSteamMobile("account", "password")

    async def unexpected_request(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("Steam time API must not be called without a proxy")

    monkeypatch.setattr(client, "_request", unexpected_request)
    asyncio.run(client.align_time())

    assert calls == 0
    assert client.is_using_steam_time() is False


def test_proxy_connector_keeps_tls_verification_enabled(monkeypatch) -> None:
    connector_calls: list[tuple[str, dict]] = []

    class FakeProxyConnector:
        pass

    class FakeSession:
        closed = False

        def __init__(self, **kwargs):
            self.connector = kwargs["connector"]

        async def close(self) -> None:
            self.closed = True

    def fake_from_url(url: str, **kwargs):
        connector_calls.append((url, kwargs))
        return FakeProxyConnector()

    monkeypatch.setattr(steam_client.ProxyConnector, "from_url", fake_from_url)
    monkeypatch.setattr(steam_client.aiohttp, "ClientSession", FakeSession)

    client = steam_client.AsyncSteamMobile(
        "account", "password", proxy="http://8.8.8.8:8080"
    )
    asyncio.run(client._ensure_session())

    assert connector_calls == [("http://8.8.8.8:8080", {"rdns": True})]


def test_every_proxy_connector_requires_remote_destination_dns() -> None:
    source_path = ROOT / "guardbot" / "steam" / "client.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    connector_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "from_url"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "ProxyConnector"
    ]

    assert len(connector_calls) == 2
    for call in connector_calls:
        assert any(
            keyword.arg == "rdns"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
            for keyword in call.keywords
        )


def test_steam_requests_never_disable_tls_verification() -> None:
    files = [
        ROOT / "guardbot/steam/client.py",
        ROOT / "guardbot/bot/runtime.py",
    ]
    offenders: list[str] = []

    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "ssl"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is False
                ):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")

    assert offenders == []


def test_every_direct_steam_session_request_rechecks_proxy_transport() -> None:
    source_path = ROOT / "guardbot" / "steam" / "client.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    offenders: list[str] = []

    for class_node in (
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name in {"AsyncSteamSession", "AsyncSteamMobile"}
    ):
        for function in (
            node
            for node in class_node.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            direct_requests = [
                node
                for node in ast.walk(function)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"get", "post", "request"}
                and isinstance(node.func.value, ast.Attribute)
                and isinstance(node.func.value.value, ast.Name)
                and node.func.value.value.id == "self"
                and node.func.value.attr == "session"
            ]
            if not direct_requests:
                continue
            checks_transport = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
                and node.func.attr == "_ensure_session"
                for node in ast.walk(function)
            )
            if not checks_transport:
                offenders.append(f"{function.name}:{function.lineno}")

    assert offenders == []


def test_proxy_parser_rejects_invalid_or_unsafe_urls() -> None:
    invalid = [
        "ftp://proxy.example:21",
        "https://proxy.example:443",
        "http://",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://169.254.169.254:8080",
        "http://proxy.example:not-a-port",
        "http://user:pass@proxy.example:8080/path",
    ]

    for value in invalid:
        with pytest.raises(ValueError):
            steam_client.parse_proxy_string(value)


def test_proxy_hostname_resolution_rejects_non_global_results(monkeypatch) -> None:
    class FakeLoop:
        async def getaddrinfo(self, *args, **kwargs):
            return [(None, None, None, None, ("127.0.0.1", 8080))]

    monkeypatch.setattr(asyncio, "get_running_loop", FakeLoop)

    with pytest.raises(ValueError, match="служебный адрес"):
        asyncio.run(
            steam_client.resolve_public_proxy_url("http://proxy.example:8080")
        )


def test_proxy_hostname_resolution_has_a_timeout(monkeypatch) -> None:
    class FakeLoop:
        async def getaddrinfo(self, *args, **kwargs):
            await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "get_running_loop", FakeLoop)
    monkeypatch.setattr(steam_client, "PROXY_DNS_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(ValueError, match="Таймаут DNS"):
        asyncio.run(
            steam_client.resolve_public_proxy_url("http://proxy.example:8080")
        )


def test_proxy_hostname_is_resolved_and_pinned(monkeypatch) -> None:
    class FakeLoop:
        async def getaddrinfo(self, *args, **kwargs):
            return [(None, None, None, None, ("93.184.216.34", 8080))]

    monkeypatch.setattr(asyncio, "get_running_loop", FakeLoop)

    assert (
        asyncio.run(
            steam_client.resolve_public_proxy_url("http://user:pass@proxy.example:8080")
        )
        == "http://user:pass@93.184.216.34:8080"
    )


def test_proxy_probe_uses_only_the_pinned_proxy_url(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    async def fake_resolve(proxy_url: str) -> str:
        calls.append(("resolve", proxy_url))
        return "http://93.184.216.34:8080"

    class FakeContent:
        async def read(self, limit: int) -> bytes:
            return b'{"ip":"198.51.100.1"}'

    class FakeResponse:
        content = FakeContent()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def raise_for_status(self) -> None:
            return None

    class FakeSession:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def get(self, url: str, *, proxy: str):
            calls.append(("connect", proxy))
            return FakeResponse()

    monkeypatch.setattr(steam_client, "resolve_public_proxy_url", fake_resolve)
    monkeypatch.setattr(steam_client.aiohttp, "ClientSession", FakeSession)

    assert (
        asyncio.run(
            steam_client.probe_proxy_external_ip("http://proxy.example:8080")
        )
        == "198.51.100.1"
    )
    assert calls == [
        ("resolve", "http://proxy.example:8080"),
        ("connect", "http://93.184.216.34:8080"),
    ]


def test_proxy_test_handlers_do_not_construct_network_sessions() -> None:
    handler_paths = [
        ROOT / "guardbot/bot/handlers/common.py",
        ROOT / "guardbot/bot/handlers/accounts.py",
    ]
    for path in handler_paths:
        source = path.read_text(encoding="utf-8")
        assert "ClientSession(" not in source
        assert "ProxyConnector.from_url(" not in source


def test_proxy_redaction_never_exposes_password() -> None:
    redacted = steam_client.redact_proxy_url(
        "socks5://proxy-user:super-secret@proxy.example:1080"
    )

    assert "super-secret" not in redacted
    assert redacted == "socks5://proxy-user:***@proxy.example:1080"


@pytest.mark.parametrize(
    "url",
    [
        "http://avatars.cloudflare.steamstatic.com/avatar.png",
        "https://evil.example/avatar.png",
        "https://127.0.0.1/avatar.png",
        "https://steamstatic.com.evil.example/avatar.png",
    ],
)
def test_steam_asset_urls_reject_untrusted_hosts(url: str) -> None:
    with pytest.raises(ValueError):
        steam_client.validate_steam_asset_url(url)


def test_steam_asset_url_accepts_https_steam_cdn() -> None:
    assert (
        steam_client.validate_steam_asset_url(
            "https://avatars.cloudflare.steamstatic.com/avatar.png"
        )
        == "https://avatars.cloudflare.steamstatic.com/avatar.png"
    )


def test_steam_asset_download_requires_proxy() -> None:
    async def scenario() -> None:
        client = steam_client.AsyncSteamMobile("account", "password")
        with pytest.raises(steam_client.ProxyRequiredError):
            await client._download_steam_asset(
                "https://avatars.cloudflare.steamstatic.com/avatar.png"
            )

    asyncio.run(scenario())


def test_market_price_request_requires_proxy_bound_client() -> None:
    from guardbot.bot.runtime import get_item_price

    async def scenario() -> None:
        client = steam_client.AsyncSteamMobile("account", "password")
        with pytest.raises(steam_client.ProxyRequiredError):
            await get_item_price(client, 730, "Item Name")

    asyncio.run(scenario())


def test_account_proxy_resolution_precedence_and_fail_closed(monkeypatch) -> None:
    from guardbot.services.session_manager import (
        ProxyRequiredError,
        resolve_proxy_for_account,
    )

    monkeypatch.setattr(
        "guardbot.services.session_manager.decrypt_value", lambda value: value
    )

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class Session:
        def __init__(self, user):
            self.user = user
            self.calls = 0

        async def execute(self, statement):
            self.calls += 1
            return Result(self.user)

    unique = SimpleNamespace(
        account_name="unique",
        telegram_id=7,
        unique_proxy=True,
        proxy="socks5://unique.example:1080",
    )
    general = SimpleNamespace(
        account_name="general",
        telegram_id=7,
        unique_proxy=False,
        proxy=None,
    )
    broken_unique = SimpleNamespace(
        account_name="fallback",
        telegram_id=7,
        unique_proxy=True,
        proxy=None,
    )

    unique_session = Session(
        SimpleNamespace(general_proxy="http://general.example:8080")
    )
    assert (
        asyncio.run(resolve_proxy_for_account(unique, unique_session))
        == "socks5://unique.example:1080"
    )
    assert unique_session.calls == 0

    general_session = Session(
        SimpleNamespace(general_proxy="http://general.example:8080")
    )
    assert (
        asyncio.run(resolve_proxy_for_account(general, general_session))
        == "http://general.example:8080"
    )

    fallback_session = Session(
        SimpleNamespace(general_proxy="http://general.example:8080")
    )
    assert (
        asyncio.run(resolve_proxy_for_account(broken_unique, fallback_session))
        == "http://general.example:8080"
    )

    missing_session = Session(SimpleNamespace(general_proxy=None))
    with pytest.raises(ProxyRequiredError):
        asyncio.run(resolve_proxy_for_account(general, missing_session))
