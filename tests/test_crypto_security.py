import ast
import asyncio
import base64
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from guardbot import security
from guardbot.passwords import (
    PasswordAttemptLimiter,
    PasswordRateLimitError,
    hash_bot_password,
    password_hash_needs_upgrade,
    verify_bot_password,
    verify_bot_password_async,
)


def test_encryption_fails_closed_when_not_initialized(monkeypatch) -> None:
    monkeypatch.setattr(security, "fernet", None)

    with pytest.raises(security.EncryptionError):
        security.encrypt_value("steam-secret")

    with pytest.raises(security.EncryptionError):
        security.encrypt_dict({"token": "steam-secret"})


def test_encryption_never_returns_plaintext_on_failure(monkeypatch) -> None:
    class BrokenFernet:
        def encrypt(self, value: bytes) -> bytes:
            raise RuntimeError("broken encryption backend")

    monkeypatch.setattr(security, "fernet", BrokenFernet())

    with pytest.raises(security.EncryptionError):
        security.encrypt_value("steam-secret")


def test_decryption_rejects_malformed_ciphertext(monkeypatch) -> None:
    monkeypatch.setattr(security, "fernet", Fernet(Fernet.generate_key()))

    with pytest.raises(security.EncryptionError):
        security.decrypt_value("plaintext-secret")

    with pytest.raises(security.EncryptionError):
        security.decrypt_dict('{"token": "plaintext-secret"}')


def test_bot_password_is_salted_and_one_way() -> None:
    first = hash_bot_password("correct horse battery staple")
    second = hash_bot_password("correct horse battery staple")

    assert first != second
    assert "correct horse battery staple" not in first
    assert verify_bot_password(first, "correct horse battery staple")
    assert not verify_bot_password(first, "wrong password")
    assert not password_hash_needs_upgrade(first)


def test_legacy_encrypted_bot_password_can_be_migrated(monkeypatch) -> None:
    monkeypatch.setattr(security, "fernet", Fernet(Fernet.generate_key()))
    legacy = security.encrypt_value("legacy-password")
    assert isinstance(legacy, str)

    assert verify_bot_password(legacy, "legacy-password")
    assert not verify_bot_password(legacy, "wrong password")
    assert password_hash_needs_upgrade(legacy)


def test_old_scrypt_parameters_are_verified_and_marked_for_upgrade() -> None:
    salt = b"0123456789abcdef"
    digest = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(b"password")
    encode = lambda value: base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
    stored = f"scrypt${2**14}$8$1${encode(salt)}${encode(digest)}"

    assert verify_bot_password(stored, "password")
    assert password_hash_needs_upgrade(stored)


def test_async_password_verification_rate_limits_each_user() -> None:
    async def scenario() -> None:
        limiter = PasswordAttemptLimiter(max_attempts=2, window_seconds=60)
        stored = hash_bot_password("correct-password")

        assert not await verify_bot_password_async(
            stored, "wrong-1", user_id=7, limiter=limiter
        )
        assert not await verify_bot_password_async(
            stored, "wrong-2", user_id=7, limiter=limiter
        )
        with pytest.raises(PasswordRateLimitError):
            await verify_bot_password_async(
                stored, "wrong-3", user_id=7, limiter=limiter
            )

    asyncio.run(scenario())


def test_handlers_do_not_store_sensitive_mafile_fields_as_plaintext() -> None:
    sensitive_fields = {
        "password",
        "shared_secret",
        "identity_secret",
        "secret_1",
        "device_id",
        "serial_number",
        "revocation_code",
        "token_gid",
        "uri",
        "access_token",
        "refresh_token",
        "proxy",
        "notes",
    }
    offenders: list[str] = []
    handlers = Path(__file__).parents[1] / "guardbot" / "bot" / "handlers"

    for source_path in handlers.glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            encrypted = (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "encrypt_value"
            )
            is_none = isinstance(value, ast.Constant) and value.value is None
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in {"mafile", "existing"}
                    and target.attr in sensitive_fields
                    and not encrypted
                    and not is_none
                ):
                    offenders.append(
                        f"{source_path.name}:{node.lineno} {target.value.id}.{target.attr}"
                    )

    assert offenders == []


def test_steam_client_does_not_log_cookies_or_session_ids() -> None:
    steam_root = Path(__file__).parents[1] / "guardbot" / "steam"
    offenders: list[str] = []

    for source_path in steam_root.rglob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            if node.func.attr not in {
                "debug",
                "info",
                "warning",
                "error",
                "critical",
            }:
                continue
            rendered = ast.unparse(node)
            forbidden_fragments = (
                "cookie.value",
                "sessionid[",
                "sessionid_before[",
                "response_text[",
                "text_preview",
                "HTML preview",
                "Full response data",
                "Raw (first",
            )
            if any(fragment in rendered for fragment in forbidden_fragments):
                offenders.append(
                    f"{source_path.relative_to(steam_root)}:{node.lineno}"
                )

    assert offenders == []


def test_steam_login_code_is_not_repeated_in_chat() -> None:
    source_path = (
        Path(__file__).parents[1] / "guardbot" / "bot" / "handlers" / "bulk_import.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    handler = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "process_steam_code"
    )
    rendered = ast.unparse(handler)

    assert "Подтверждаю вход с кодом" not in rendered
    assert "await message.delete()" in rendered
