import asyncio
import gc
from types import SimpleNamespace

import pytest
from aiogram.fsm.storage.base import StorageKey


class _Client:
    def __init__(self) -> None:
        self.closed = False
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()
        self.block_close = False

    async def close(self) -> None:
        self.close_started.set()
        if self.block_close:
            await self.allow_close.wait()
        self.closed = True


def test_concurrent_legacy_replacements_keep_latest_and_close_displaced_resources() -> None:
    from guardbot.bot import runtime

    async def scenario() -> None:
        user_id = 9101
        old = _Client()
        old.block_close = True
        first = _Client()
        second = _Client()
        runtime.user_states[user_id] = {"state": "waiting_email_code", "client": old}

        replace_first = asyncio.create_task(
            runtime.set_legacy_user_state(
                user_id,
                {"state": "waiting_email_code", "client": first},
                ttl_seconds=60,
            )
        )
        await old.close_started.wait()
        replace_second = asyncio.create_task(
            runtime.set_legacy_user_state(
                user_id,
                {"state": "waiting_email_code", "client": second},
                ttl_seconds=60,
            )
        )
        await asyncio.sleep(0)
        old.allow_close.set()
        await asyncio.gather(replace_first, replace_second)

        try:
            assert runtime.user_states[user_id]["client"] is second
            assert old.closed is True
            assert first.closed is True
            assert second.closed is False
        finally:
            await runtime.clear_legacy_user_state(user_id)

    asyncio.run(scenario())


def test_legacy_state_replacement_finishes_cleanup_before_cancellation_escapes() -> None:
    from guardbot.bot import runtime

    async def scenario() -> None:
        user_id = 9102
        old = _Client()
        old.block_close = True
        replacement = _Client()
        runtime.user_states[user_id] = {"state": "waiting_email_code", "client": old}

        task = asyncio.create_task(
            runtime.set_legacy_user_state(
                user_id,
                {"state": "waiting_email_code", "client": replacement},
                ttl_seconds=60,
            )
        )
        await old.close_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        old.allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        try:
            assert old.closed is True
            assert runtime.user_states[user_id]["client"] is replacement
        finally:
            await runtime.clear_legacy_user_state(user_id)

    asyncio.run(scenario())


def test_secret_state_expiry_is_paused_while_handler_uses_client() -> None:
    from guardbot.bot import runtime

    async def scenario() -> None:
        user_id = 9103
        client = _Client()
        await runtime.set_legacy_user_state(
            user_id,
            {"state": "waiting_steam_code", "client": client},
            ttl_seconds=0.01,
        )
        event = SimpleNamespace(
            message=SimpleNamespace(from_user=SimpleNamespace(id=user_id)),
            callback_query=None,
            inline_query=None,
        )
        entered = asyncio.Event()
        release = asyncio.Event()

        async def handler(_event, _data):
            entered.set()
            await release.wait()
            assert client.closed is False

        task = asyncio.create_task(runtime.SecretStateCleanupMiddleware()(handler, event, {}))
        await entered.wait()
        await asyncio.sleep(0.03)
        assert client.closed is False
        release.set()
        await task
        await asyncio.sleep(0.03)
        assert client.closed is True
        assert user_id not in runtime.user_states

    asyncio.run(scenario())


def test_secret_state_is_cleaned_when_handler_is_cancelled() -> None:
    from guardbot.bot import runtime

    async def scenario() -> None:
        user_id = 9104
        client = _Client()
        await runtime.set_legacy_user_state(
            user_id,
            {"state": "waiting_email_code", "client": client},
            ttl_seconds=60,
        )
        event = SimpleNamespace(
            message=SimpleNamespace(from_user=SimpleNamespace(id=user_id)),
            callback_query=None,
            inline_query=None,
        )
        entered = asyncio.Event()

        async def handler(_event, _data):
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(runtime.SecretStateCleanupMiddleware()(handler, event, {}))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert user_id not in runtime.user_states
        assert client.closed is True

    asyncio.run(scenario())


def test_replacement_rejects_resource_that_is_already_being_retired() -> None:
    from guardbot.bot import runtime

    async def scenario() -> None:
        user_id = 9105
        first = _Client()
        first.block_close = True
        second = _Client()
        runtime.user_states[user_id] = {"state": "waiting_email_code", "client": first}

        replace = asyncio.create_task(
            runtime.set_legacy_user_state(
                user_id,
                {"state": "waiting_email_code", "client": second},
                ttl_seconds=60,
            )
        )
        await first.close_started.wait()
        with pytest.raises(RuntimeError, match="retired"):
            await runtime.set_legacy_user_state(
                user_id,
                {"state": "waiting_email_code", "client": first},
                ttl_seconds=60,
            )
        first.allow_close.set()
        await replace

        try:
            assert runtime.user_states[user_id]["client"] is second
            assert first.closed is True
            assert second.closed is False
        finally:
            await runtime.clear_legacy_user_state(user_id)

    asyncio.run(scenario())


def test_legacy_cleanup_resists_repeated_cancellation() -> None:
    from guardbot.bot import runtime

    async def scenario() -> None:
        user_id = 9106
        old = _Client()
        old.block_close = True
        replacement = _Client()
        runtime.user_states[user_id] = {"state": "waiting_email_code", "client": old}

        task = asyncio.create_task(
            runtime.set_legacy_user_state(
                user_id,
                {"state": "waiting_email_code", "client": replacement},
                ttl_seconds=60,
            )
        )
        await old.close_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False
        old.allow_close.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert old.closed is True

        await runtime.clear_legacy_user_state(user_id)

    asyncio.run(scenario())


def test_lease_lookup_does_not_retain_locks_for_users_without_state() -> None:
    from guardbot.bot import runtime

    async def scenario() -> None:
        before = len(runtime._legacy_state_locks)
        for user_id in range(20_000, 21_000):
            assert await runtime.lease_legacy_user_state(user_id) is None
        gc.collect()
        assert len(runtime._legacy_state_locks) <= before

    asyncio.run(scenario())


def test_event_isolation_evicts_idle_user_locks() -> None:
    from guardbot.bot import runtime

    async def scenario() -> None:
        isolation = runtime.EvictingEventIsolation()
        for user_id in range(30_000, 31_000):
            key = StorageKey(bot_id=1, chat_id=user_id, user_id=user_id)
            async with isolation.lock(key):
                pass
        assert isolation.lock_count == 0
        await isolation.close()

    asyncio.run(scenario())


def test_cancelled_handler_does_not_clear_concurrent_replacement_state() -> None:
    from guardbot.bot import runtime

    async def scenario() -> None:
        user_id = 9107
        first = _Client()
        second = _Client()
        await runtime.set_legacy_user_state(
            user_id,
            {"state": "waiting_email_code", "client": first},
            ttl_seconds=60,
        )
        event = SimpleNamespace(
            message=SimpleNamespace(from_user=SimpleNamespace(id=user_id)),
            callback_query=None,
            inline_query=None,
        )
        entered = asyncio.Event()

        async def handler(_event, _data):
            entered.set()
            await asyncio.Event().wait()

        middleware_task = asyncio.create_task(
            runtime.SecretStateCleanupMiddleware()(handler, event, {})
        )
        await entered.wait()
        replacement = {"state": "waiting_email_code", "client": second}
        await runtime.set_legacy_user_state(user_id, replacement, ttl_seconds=60)
        middleware_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await middleware_task

        try:
            assert runtime.user_states[user_id] is replacement
            assert second.closed is False
        finally:
            await runtime.clear_legacy_user_state(user_id)

    asyncio.run(scenario())


def test_event_isolation_evicts_key_when_cleanup_is_cancelled() -> None:
    from guardbot.bot import runtime

    async def scenario() -> None:
        isolation = runtime.EvictingEventIsolation()
        key = StorageKey(bot_id=1, chat_id=92_001, user_id=92_001)
        entered = asyncio.Event()
        leave = asyncio.Event()

        async def owner() -> None:
            async with isolation.lock(key):
                entered.set()
                await leave.wait()

        task = asyncio.create_task(owner())
        await entered.wait()
        await isolation._guard.acquire()
        leave.set()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        isolation._guard.release()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert isolation.lock_count == 0

    asyncio.run(scenario())


def test_secret_state_lease_release_rearms_ttl_when_cancelled() -> None:
    from guardbot.bot import runtime

    async def scenario() -> None:
        user_id = 92_002
        client = _Client()
        await runtime.set_legacy_user_state(
            user_id,
            {"state": "waiting_email_code", "client": client},
            ttl_seconds=60,
        )
        entered = asyncio.Event()
        finish = asyncio.Event()
        event = SimpleNamespace(
            message=SimpleNamespace(from_user=SimpleNamespace(id=user_id)),
            callback_query=None,
            inline_query=None,
        )

        async def handler(_event, _data):
            entered.set()
            await finish.wait()

        task = asyncio.create_task(
            runtime.SecretStateCleanupMiddleware()(handler, event, {})
        )
        await entered.wait()
        lock = runtime._legacy_state_lock(user_id)
        await lock.acquire()
        finish.set()
        await asyncio.sleep(0)
        task.cancel()
        lock.release()
        with pytest.raises(asyncio.CancelledError):
            await task

        try:
            state_data = runtime.user_states[user_id]
            assert state_data.get("_expiry_task") is not None
        finally:
            await runtime.clear_legacy_user_state(user_id)

    asyncio.run(scenario())


def test_event_isolation_evicts_cancelled_waiter_reference() -> None:
    from guardbot.bot import runtime

    async def scenario() -> None:
        isolation = runtime.EvictingEventIsolation()
        key = StorageKey(bot_id=1, chat_id=92_003, user_id=92_003)
        owner_entered = asyncio.Event()
        release_owner = asyncio.Event()

        async def owner() -> None:
            async with isolation.lock(key):
                owner_entered.set()
                await release_owner.wait()

        async def waiter() -> None:
            async with isolation.lock(key):
                raise AssertionError("cancelled waiter must not acquire the key")

        owner_task = asyncio.create_task(owner())
        await owner_entered.wait()
        waiter_task = asyncio.create_task(waiter())
        while isolation._entries[key][1] != 2:
            await asyncio.sleep(0)
        waiter_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter_task

        assert isolation._entries[key][1] == 1
        release_owner.set()
        await owner_task
        assert isolation.lock_count == 0

    asyncio.run(scenario())
