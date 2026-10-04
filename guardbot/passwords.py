"""One-way hashing and migration helpers for the bot lock password."""

import asyncio
import base64
import hmac
import os
import time

from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from guardbot.security import EncryptionError, decrypt_value

_SCHEME = "scrypt"
MIN_BOT_PASSWORD_LENGTH = 8
_N = 2**15
_R = 8
_P = 1
_LENGTH = 32
_SUPPORTED_PARAMETERS = {(2**14, _R, _P), (_N, _R, _P)}
_KDF_SEMAPHORE = asyncio.Semaphore(2)


class PasswordRateLimitError(RuntimeError):
    """Raised when one user exceeds the bounded password-attempt window."""


class PasswordAttemptLimiter:
    """Small in-memory per-user limiter that bounds expensive KDF work."""

    def __init__(self, max_attempts: int = 5, window_seconds: float = 60) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: dict[int, list[float]] = {}

    def begin_attempt(self, user_id: int) -> None:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        attempts = [
            value for value in self._attempts.get(user_id, []) if value > cutoff
        ]
        if len(attempts) >= self.max_attempts:
            self._attempts[user_id] = attempts
            raise PasswordRateLimitError("Too many password attempts")
        attempts.append(now)
        self._attempts[user_id] = attempts

    def record_success(self, user_id: int) -> None:
        self._attempts.pop(user_id, None)


_PASSWORD_ATTEMPT_LIMITER = PasswordAttemptLimiter()


def hash_bot_password(password: str) -> str:
    """Return a salted, one-way scrypt hash suitable for database storage."""
    if not password:
        raise ValueError("Password must not be empty")
    salt = os.urandom(16)
    digest = _derive(password, salt, _N, _R, _P)
    return "$".join(
        (
            _SCHEME,
            str(_N),
            str(_R),
            str(_P),
            _encode(salt),
            _encode(digest),
        )
    )


def verify_bot_password(stored_value: str, password: str) -> bool:
    """Verify a current scrypt hash or a legacy Fernet-encrypted password."""
    if not stored_value:
        return False
    if stored_value.startswith(f"{_SCHEME}$"):
        try:
            scheme, n, r, p, salt, expected = stored_value.split("$", 5)
            if scheme != _SCHEME:
                return False
            actual = _derive(password, _decode(salt), int(n), int(r), int(p))
            return hmac.compare_digest(actual, _decode(expected))
        except (TypeError, ValueError):
            return False

    # Migration path for values written by older versions.
    try:
        legacy_password = decrypt_value(stored_value)
    except EncryptionError:
        return False
    return hmac.compare_digest(password, legacy_password or "")


async def hash_bot_password_async(password: str) -> str:
    """Hash without blocking the asyncio event loop or oversubscribing memory."""
    async with _KDF_SEMAPHORE:
        return await asyncio.to_thread(hash_bot_password, password)


async def verify_bot_password_async(
    stored_value: str,
    password: str,
    *,
    user_id: int,
    limiter: PasswordAttemptLimiter = _PASSWORD_ATTEMPT_LIMITER,
) -> bool:
    """Rate-limit and verify a password in a bounded worker thread."""
    limiter.begin_attempt(user_id)
    async with _KDF_SEMAPHORE:
        valid = await asyncio.to_thread(verify_bot_password, stored_value, password)
    if valid:
        limiter.record_success(user_id)
    return valid


def password_hash_needs_upgrade(stored_value: str) -> bool:
    """Return True for valid legacy formats that should be re-hashed after login."""
    if not stored_value.startswith(f"{_SCHEME}$"):
        return True
    try:
        scheme, n, r, p, _, _ = stored_value.split("$", 5)
        return scheme != _SCHEME or (int(n), int(r), int(p)) != (_N, _R, _P)
    except (TypeError, ValueError):
        return True


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    # Reject attacker-controlled pathological parameters from a corrupted DB row.
    if (n, r, p) not in _SUPPORTED_PARAMETERS or len(salt) != 16:
        raise ValueError("Unsupported password hash parameters")
    kdf = Scrypt(salt=salt, length=_LENGTH, n=n, r=r, p=p)
    return kdf.derive(password.encode("utf-8"))


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
