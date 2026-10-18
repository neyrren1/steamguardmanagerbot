"""TTL-bound ownership registry for live Steam login challenge clients."""

import asyncio
import inspect
import logging
import secrets
from dataclasses import dataclass, field
from typing import Any
from weakref import WeakValueDictionary

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PendingChallenge:
    token: str
    user_id: int
    mafile_id: int
    kind: str
    client: Any
    ttl_seconds: float
    leased: bool = False
    owner_task: asyncio.Task | None = field(default=None, repr=False)
    state: Any = field(default=None, repr=False)
    expiry_task: asyncio.Task | None = field(default=None, repr=False)


_entries: dict[str, PendingChallenge] = {}
_current_tokens: dict[int, str] = {}
_guard = asyncio.Lock()
_user_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()


def _user_lock(user_id: int) -> asyncio.Lock:
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock


async def _await_cancellation_safe(awaitable):
    """Finish an ownership transition before propagating task cancellation."""
    task = asyncio.create_task(awaitable)
    cancellation_requested = False
    current_task = asyncio.current_task()
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancellation_requested = True
            if current_task is not None:
                current_task.uncancel()
    if cancellation_requested:
        raise asyncio.CancelledError
    return task.result()


async def _close_client(entry: PendingChallenge) -> None:
    close = getattr(entry.client, "close", None)
    if close is None:
        return
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "Failed to close pending Steam challenge client for user %s: %s",
            entry.user_id,
            type(exc).__name__,
        )


def _schedule_expiry(entry: PendingChallenge) -> asyncio.Task:
    return asyncio.create_task(
        _expire_entry(entry.token, entry, entry.ttl_seconds),
        name=f"steam-challenge-expiry:{entry.token}",
    )


async def _expire_entry(
    token: str,
    entry: PendingChallenge,
    ttl_seconds: float,
) -> None:
    """Expire one exact challenge and its FSM binding under the lifecycle lock."""
    await asyncio.sleep(ttl_seconds)
    expiry_task = asyncio.current_task()
    async with _user_lock(entry.user_id):
        async with _guard:
            if (
                _entries.get(token) is not entry
                or entry.leased
                or entry.expiry_task is not expiry_task
            ):
                return
            _entries.pop(token, None)
            was_current = _current_tokens.get(entry.user_id) == token
            if was_current:
                _current_tokens.pop(entry.user_id, None)
            entry.expiry_task = None

        async def close_and_unbind() -> None:
            await _close_client(entry)
            if entry.state is not None:
                await _clear_state_if_bound(entry.state, token)

        await _await_cancellation_safe(close_and_unbind())


async def _clear_state_if_bound(state: Any, token: str) -> None:
    """Clear FSM only while it still references this exact challenge token."""
    data = await state.get_data()
    if data.get("challenge_id") == token:
        await state.clear()


async def _store_pending_challenge(
    *,
    user_id: int,
    mafile_id: int,
    kind: str,
    client: Any,
    ttl_seconds: float = 600,
    state: Any = None,
) -> str:
    """Register a live client; callers must hold the per-user lifecycle lock.

    Production code uses publish_pending_challenge, which binds registry and FSM
    atomically. This primitive stays private so no caller can bypass that lock.
    """
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("invalid user_id")
    if type(mafile_id) is not int or mafile_id <= 0:
        raise ValueError("invalid mafile_id")
    if not kind or len(kind) > 64:
        raise ValueError("invalid challenge kind")
    if ttl_seconds <= 0:
        raise ValueError("invalid challenge TTL")

    while True:
        token = secrets.token_hex(16)
        async with _guard:
            if token in _entries:
                continue
            entry = PendingChallenge(
                token=token,
                user_id=user_id,
                mafile_id=mafile_id,
                kind=kind,
                client=client,
                ttl_seconds=ttl_seconds,
                owner_task=asyncio.current_task(),
                state=state,
            )
            entry.expiry_task = _schedule_expiry(entry)
            _entries[token] = entry
            _current_tokens[user_id] = token
            return token


async def _remove_user_entries(
    user_id: int,
    *,
    keep_token: str | None = None,
) -> None:
    async def remove_and_close() -> None:
        async with _guard:
            entries = [
                entry
                for entry in _entries.values()
                if entry.user_id == user_id and entry.token != keep_token
            ]
            if keep_token is not None and any(entry.leased for entry in entries):
                raise RuntimeError("Cannot replace a leased pending challenge")
            for entry in entries:
                _entries.pop(entry.token, None)
                expiry_task = entry.expiry_task
                entry.expiry_task = None
                if expiry_task is not None and expiry_task is not asyncio.current_task():
                    expiry_task.cancel()
            if keep_token is None:
                _current_tokens.pop(user_id, None)
            elif keep_token in _entries:
                _current_tokens[user_id] = keep_token
        for entry in entries:
            await _close_client(entry)

    await _await_cancellation_safe(remove_and_close())


async def publish_pending_challenge(
    *,
    user_id: int,
    mafile_id: int,
    kind: str,
    client: Any,
    state: Any,
    fsm_state: Any,
    ttl_seconds: float = 600,
) -> str:
    """Atomically bind one live challenge to its user FSM state."""
    lock = _user_lock(user_id)
    try:
        await lock.acquire()
    except BaseException:
        await _await_cancellation_safe(
            _close_client(
                PendingChallenge(
                    token="",
                    user_id=user_id,
                    mafile_id=mafile_id,
                    kind=kind,
                    client=client,
                    ttl_seconds=ttl_seconds,
                )
            )
        )
        raise

    try:
        previous_token = _current_tokens.get(user_id)
        previous_data: dict = {}
        previous_state: Any = None
        snapshot_complete = False
        token: str | None = None
        try:
            previous_data = await state.get_data()
            previous_state = await state.get_state()
            snapshot_complete = True
            token = await _store_pending_challenge(
                user_id=user_id,
                mafile_id=mafile_id,
                kind=kind,
                client=client,
                ttl_seconds=ttl_seconds,
                state=state,
            )
            await state.set_data(
                {
                    "challenge_id": token,
                    "mafile_id": mafile_id,
                    "kind": kind,
                }
            )
            await state.set_state(fsm_state)
            await _remove_user_entries(user_id, keep_token=token)
        except BaseException:

            async def rollback() -> None:
                if token is None:
                    await _close_client(
                        PendingChallenge(
                            token="",
                            user_id=user_id,
                            mafile_id=mafile_id,
                            kind=kind,
                            client=client,
                            ttl_seconds=ttl_seconds,
                        )
                    )
                else:
                    await remove_pending_challenge(token)
                can_restore_previous = previous_token is None
                if previous_token is not None:
                    async with _guard:
                        if previous_token in _entries:
                            _current_tokens[user_id] = previous_token
                            can_restore_previous = True
                if snapshot_complete:
                    if can_restore_previous:
                        await state.set_data(previous_data)
                        await state.set_state(previous_state)
                    else:
                        await state.clear()

            await _await_cancellation_safe(rollback())
            raise

        return token
    finally:
        lock.release()


async def read_pending_challenge_data(user_id: int, state: Any) -> dict:
    """Read challenge FSM data and clean only its exact resource on interruption."""
    token = _current_tokens.get(user_id)
    lock = _user_lock(user_id)
    try:
        await lock.acquire()
    except BaseException:
        if token is not None:
            await clear_owned_pending_challenge(
                token,
                state,
                user_id=user_id,
            )
        raise

    lock_acquired = True
    try:
        return await state.get_data()
    except BaseException:
        lock.release()
        lock_acquired = False
        if token is not None:
            await clear_owned_pending_challenge(
                token,
                state,
                user_id=user_id,
            )
        raise
    finally:
        if lock_acquired:
            lock.release()


async def lease_pending_challenge(
    token: str,
    *,
    user_id: int,
    mafile_id: int,
    kind: str,
) -> PendingChallenge | None:
    """Pause expiry while one handler owns and uses a matching challenge."""
    async with _guard:
        entry = _entries.get(token)
        if (
            entry is None
            or entry.user_id != user_id
            or entry.mafile_id != mafile_id
            or entry.kind != kind
            or entry.leased
        ):
            return None
        entry.leased = True
        expiry_task = entry.expiry_task
        entry.expiry_task = None
        if expiry_task is not None:
            expiry_task.cancel()
        return entry


async def release_pending_challenge(
    token: str,
    entry: PendingChallenge,
) -> None:
    """Restore TTL when a retryable handler returns the same live challenge."""
    async with _guard:
        if _entries.get(token) is not entry:
            return
        entry.leased = False
        if entry.expiry_task is None:
            entry.expiry_task = _schedule_expiry(entry)


async def remove_pending_challenge(
    token: str,
    *,
    expected_entry: PendingChallenge | None = None,
) -> None:
    """Remove one exact challenge and close its client cancellation-safely.

    A leased challenge belongs to the handler that leased it. Only that handler,
    identified by passing the leased entry as ``expected_entry``, may remove it;
    every other caller leaves it untouched so an in-flight Steam request is never
    interrupted.
    """

    async def remove_and_close() -> None:
        async with _guard:
            entry = _entries.get(token)
            if entry is None or (
                expected_entry is not None and entry is not expected_entry
            ):
                return
            if entry.leased and expected_entry is not entry:
                return
            _entries.pop(token, None)
            if _current_tokens.get(entry.user_id) == token:
                _current_tokens.pop(entry.user_id, None)
            expiry_task = entry.expiry_task
            entry.expiry_task = None
            if expiry_task is not None and expiry_task is not asyncio.current_task():
                expiry_task.cancel()
        await _close_client(entry)

    await _await_cancellation_safe(remove_and_close())


async def clear_pending_challenges_for_user(
    user_id: int,
    state: Any | None = None,
) -> None:
    """Atomically clear every live challenge and optional FSM state for a user."""

    async def clear_and_close() -> None:
        async with _user_lock(user_id):
            await _remove_user_entries(user_id)
            if state is not None:
                await state.clear()

    await _await_cancellation_safe(clear_and_close())


async def clear_owned_pending_challenge(
    token: str,
    state: Any,
    *,
    user_id: int,
    expected_entry: PendingChallenge | None = None,
) -> None:
    """Atomically clear one challenge without deleting a replacement flow."""

    async def clear_owned() -> None:
        async with _user_lock(user_id):
            entry = _entries.get(token)
            if entry is not None and entry.leased and entry is not expected_entry:
                # Another handler owns the live client; it keeps responsibility
                # for both the registry entry and the FSM binding.
                return
            owns_current_flow = _current_tokens.get(user_id) == token
            await remove_pending_challenge(token, expected_entry=expected_entry)
            if owns_current_flow:
                await state.clear()
            else:
                # Registry writers are excluded by the lifecycle lock, so stale
                # metadata of an already expired/removed challenge can be
                # compare-and-cleared without touching a replacement binding.
                await _clear_state_if_bound(state, token)

    await _await_cancellation_safe(clear_owned())


def owned_pending_challenge_token(
    user_id: int,
    owner_task: asyncio.Task | None,
) -> str | None:
    """Return the exact challenge published by one handler task, if still live."""
    if owner_task is None:
        return None
    for token, entry in _entries.items():
        if entry.user_id == user_id and entry.owner_task is owner_task:
            return token
    return None


def current_pending_challenge_token(user_id: int) -> str | None:
    """Snapshot the current token without yielding to another event-loop task."""
    return _current_tokens.get(user_id)


def pending_challenge_is_leased(token: str) -> bool:
    """Report whether another handler currently owns this challenge's client."""
    entry = _entries.get(token)
    return entry is not None and entry.leased


def pending_challenge_count() -> int:
    """Return the current registry size for health checks and regression tests."""
    return len(_entries)
