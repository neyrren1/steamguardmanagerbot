"""Extracted from the legacy bot module without behavior changes."""

from contextlib import asynccontextmanager, suppress
from decimal import Decimal, InvalidOperation
from html import escape
import inspect

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import BaseEventIsolation, StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.strategy import FSMStrategy
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from datetime import UTC, datetime
from guardbot import security
from guardbot.bot.main_menu import build_main_menu_view, edit_main_menu
from guardbot.bot.states import (
    AccountAddFlow,
    AccountMaFileImportFlow,
    BotPasswordFlow,
    BulkImportFlow,
    CustomPriceFlow,
    LoginPasswordFlow,
    NoteFlow,
    ProxyFlow,
    UnlockFlow,
)
from guardbot.config import STEAM_CURRENCIES, logger
from guardbot.database import AsyncSessionLocal, Mafile, User
from guardbot.security import decrypt_value
from guardbot.services.ownership import get_owned_mafile
from guardbot.services.session_manager import SteamSessionManager
from guardbot.steam.client import AsyncSteamMobile, ProxyRequiredError, USER_AGENT_MOBILE
from sqlalchemy import func, select, text
from typing import Optional
import aiohttp
import asyncio
import json
import math
import weakref
import re

# ==================== БОТ ====================
class EvictingEventIsolation(BaseEventIsolation):
    """Per-key event isolation that releases idle lock entries."""

    def __init__(self) -> None:
        self._entries: dict[StorageKey, tuple[asyncio.Lock, int]] = {}
        self._guard = asyncio.Lock()

    @property
    def lock_count(self) -> int:
        return len(self._entries)

    @asynccontextmanager
    async def lock(self, key: StorageKey):
        async with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                lock = asyncio.Lock()
                references = 0
            else:
                lock, references = entry
            self._entries[key] = (lock, references + 1)

        try:
            async with lock:
                yield
        finally:
            await _await_cancellation_safe(self._release_reference(key, lock))

    async def _release_reference(
        self,
        key: StorageKey,
        lock: asyncio.Lock,
    ) -> None:
        async with self._guard:
            current = self._entries.get(key)
            if current is not None and current[0] is lock:
                remaining = current[1] - 1
                if remaining == 0:
                    self._entries.pop(key, None)
                else:
                    self._entries[key] = (lock, remaining)

    async def close(self) -> None:
        async with self._guard:
            self._entries.clear()


class BotProxy:
    def __init__(self) -> None:
        self._instance: Optional[Bot] = None

    def configure(self, token: str) -> None:
        self._instance = Bot(token=token)

    def get(self) -> Bot:
        if self._instance is None:
            raise RuntimeError("Telegram bot is not initialized")
        return self._instance

    def __getattr__(self, name: str):
        return getattr(self.get(), name)


bot = BotProxy()
_UNLOCK_COMMAND_FILTER = Command("unlock")
_SET_PASSWORD_COMMAND_FILTER = Command("set_password")
_SET_PROXY_COMMAND_FILTER = Command("set_proxy")
_SET_ACCOUNT_PROXY_COMMAND_FILTER = Command("set_account_proxy")
_IMPORT_MAFILE_COMMAND_FILTER = Command("import_mafile")
_BULK_IMPORT_COMMAND_FILTER = Command("import_bulk", "mass_import", "bulk_import")
_LOGIN_COMMAND_FILTER = Command("login", "signin")
_NOTE_COMMAND_FILTER = Command("note")
_CANCEL_COMMAND_FILTER = Command("cancel")
_NAVIGATION_COMMAND_FILTER = Command(
    "start",
    "help",
    "menu",
    "commands",
    "rich_demo",
)
_ANY_COMMAND_FILTER = Command(re.compile(r".+"))
_SECRET_DOCUMENT_SUFFIXES = (".json", ".mafile", ".txt", ".zip")
_SECRET_PROMPT_CALLBACKS = {"bulk_import_menu"}
_SECRET_PROMPT_CALLBACK_PREFIXES = (
    "action_login_",
    "change_unique_proxy_",
    "enter_password_",
    "import_mafile_for_",
    "manual_code_",
    "set_unique_proxy_",
    "setup_steamguard_",
)
_SECRET_LEGACY_STATES = {
    "waiting_email_code",
    "waiting_mafile",
    "waiting_steam_code",
    "waiting_steam_code_manual",
    "waiting_steam_code_or_mafile",
}
_SECRET_FSM_STATES = {
    AccountAddFlow.waiting_credentials.state,
    AccountMaFileImportFlow.waiting_document.state,
    BotPasswordFlow.waiting_password.state,
    BulkImportFlow.waiting_credentials.state,
    LoginPasswordFlow.waiting_password.state,
    NoteFlow.waiting_note.state,
    ProxyFlow.waiting_proxy.state,
    UnlockFlow.waiting_password.state,
}

_legacy_state_locks: weakref.WeakValueDictionary[int, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)
_retiring_legacy_resource_ids: dict[int, set[int]] = {}


def _legacy_state_lock(user_id: int) -> asyncio.Lock:
    return _legacy_state_locks.setdefault(user_id, asyncio.Lock())


async def _await_cancellation_safe(awaitable):
    """Finish resource ownership transitions before propagating cancellation."""
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


def _legacy_resource_ids(state_data: dict) -> set[int]:
    return {
        id(resource)
        for key in ("client", "db_session")
        if (resource := state_data.get(key)) is not None
    }


def _mark_retiring_resources(user_id: int, resource_ids: set[int]) -> None:
    if resource_ids:
        _retiring_legacy_resource_ids.setdefault(user_id, set()).update(resource_ids)


def _unmark_retiring_resources(user_id: int, resource_ids: set[int]) -> None:
    retiring = _retiring_legacy_resource_ids.get(user_id)
    if retiring is None:
        return
    retiring.difference_update(resource_ids)
    if not retiring:
        _retiring_legacy_resource_ids.pop(user_id, None)


async def _close_legacy_state_resources(
    user_id: int,
    state_data: dict,
    *,
    preserve_resource_ids: set[int] | None = None,
) -> None:
    closed_ids: set[int] = set()
    for key in ("client", "db_session"):
        resource = state_data.get(key)
        if resource is None or id(resource) in closed_ids:
            continue
        if preserve_resource_ids and id(resource) in preserve_resource_ids:
            continue
        closed_ids.add(id(resource))
        close = getattr(resource, "close", None)
        if close is None:
            continue
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Failed to close legacy state resource %s for user %s: %s",
                key,
                user_id,
                type(exc).__name__,
            )


async def close_owned_resource(resource) -> None:
    """Close a live resource before cancellation can escape its owner."""
    async def close() -> None:
        close_method = getattr(resource, "close", None)
        if close_method is None:
            return
        result = close_method()
        if inspect.isawaitable(result):
            await result

    await _await_cancellation_safe(close())


def _schedule_legacy_state_expiry(
    user_id: int,
    state_data: dict,
    ttl_seconds: float,
) -> asyncio.Task:
    async def expire_state() -> None:
        try:
            await asyncio.sleep(ttl_seconds)
        except asyncio.CancelledError:
            return
        await _clear_legacy_user_state_impl(user_id, expected_state=state_data)

    return asyncio.create_task(expire_state())


async def _clear_legacy_user_state_impl(
    user_id: int,
    *,
    preserve_resource_ids: set[int] | None = None,
    expected_state: dict | None = None,
) -> None:
    if user_id not in user_states:
        return

    async with _legacy_state_lock(user_id):
        state_data = user_states.get(user_id)
        if not state_data or (
            expected_state is not None and state_data is not expected_state
        ):
            return
        user_states.pop(user_id, None)
        expiry_task = state_data.pop("_expiry_task", None)
        current_task = asyncio.current_task()
        if expiry_task is not None and expiry_task is not current_task:
            expiry_task.cancel()
        retiring_ids = _legacy_resource_ids(state_data)
        if preserve_resource_ids:
            retiring_ids.difference_update(preserve_resource_ids)
        _mark_retiring_resources(user_id, retiring_ids)

    async def close_resources() -> None:
        try:
            await _close_legacy_state_resources(
                user_id,
                state_data,
                preserve_resource_ids=preserve_resource_ids,
            )
        finally:
            _unmark_retiring_resources(user_id, retiring_ids)

    await _await_cancellation_safe(close_resources())


async def clear_legacy_user_state(
    user_id: int,
    *,
    preserve_resource_ids: set[int] | None = None,
    expected_state: dict | None = None,
) -> None:
    """Clear a legacy flow and close live resources it stored."""
    await _await_cancellation_safe(
        _clear_legacy_user_state_impl(
            user_id,
            preserve_resource_ids=preserve_resource_ids,
            expected_state=expected_state,
        )
    )


async def clear_user_workflows(user_id: int, state: FSMContext | None) -> None:
    """Cancellation-safely clear both transitional workflow stores."""

    async def clear() -> None:
        await clear_legacy_user_state(user_id)
        if state is not None:
            await state.clear()

    await _await_cancellation_safe(clear())


async def set_legacy_user_state(
    user_id: int,
    state_data: dict,
    *,
    ttl_seconds: float = 600,
) -> None:
    """Replace a legacy secret flow and expire its resources automatically."""
    state_data["_owner_task"] = asyncio.current_task()

    async def replace() -> None:
        preserve_resource_ids = _legacy_resource_ids(state_data)
        async with _legacy_state_lock(user_id):
            retiring = _retiring_legacy_resource_ids.get(user_id, set())
            if preserve_resource_ids & retiring:
                raise RuntimeError("Cannot reuse a legacy resource being retired")
            previous_state = user_states.get(user_id)
            if previous_state:
                previous_expiry = previous_state.pop("_expiry_task", None)
                if previous_expiry is not None:
                    previous_expiry.cancel()
            state_data["_expiry_ttl_seconds"] = ttl_seconds
            state_data["_expiry_task"] = _schedule_legacy_state_expiry(
                user_id,
                state_data,
                ttl_seconds,
            )
            user_states[user_id] = state_data
            retiring_ids = (
                _legacy_resource_ids(previous_state) - preserve_resource_ids
                if previous_state
                else set()
            )
            _mark_retiring_resources(user_id, retiring_ids)

        if previous_state:
            try:
                await _close_legacy_state_resources(
                    user_id,
                    previous_state,
                    preserve_resource_ids=preserve_resource_ids,
                )
            finally:
                _unmark_retiring_resources(user_id, retiring_ids)

    await _await_cancellation_safe(replace())


async def lease_legacy_user_state(user_id: int) -> dict | None:
    """Pause expiry while an update actively consumes the current state."""
    lock = _legacy_state_locks.get(user_id)
    if lock is None and user_id not in user_states:
        return None
    lock = lock or _legacy_state_lock(user_id)
    async with lock:
        state_data = user_states.get(user_id)
        if not state_data or state_data.get("state") not in _SECRET_LEGACY_STATES:
            return None
        expiry_task = state_data.pop("_expiry_task", None)
        if expiry_task is not None:
            expiry_task.cancel()
        return state_data


async def release_legacy_user_state_lease(user_id: int, state_data: dict) -> None:
    """Re-arm expiry when the leased state still owns the workflow."""
    async with _legacy_state_lock(user_id):
        if user_states.get(user_id) is not state_data:
            return
        if state_data.get("_expiry_task") is not None:
            return
        ttl_seconds = state_data.get("_expiry_ttl_seconds", 600)
        state_data["_expiry_task"] = _schedule_legacy_state_expiry(
            user_id,
            state_data,
            ttl_seconds,
        )


async def delete_secret_message_or_abort(message: Message) -> bool:
    """Delete secret-bearing input; refuse processing if deletion fails."""
    if getattr(message, "_guardbot_secret_deleted", False):
        return True
    delete = getattr(message, "delete", None)
    if delete is None:
        return True
    try:
        await delete()
        setattr(message, "_guardbot_secret_deleted", True)
        return True
    except Exception:
        with suppress(Exception):
            await message.answer(
                "❌ Не удалось удалить сообщение с секретными данными. "
                "Данные не были обработаны; повторите попытку в личном чате."
            )
        return False


async def _clear_typed_secret_state(fsm_context: FSMContext) -> None:
    current_state = await fsm_context.get_state()
    if current_state in _SECRET_FSM_STATES:
        await fsm_context.clear()


def init_bot():
    """Инициализация бота (вызывается после init_fernet)."""
    bot.configure(security.BOT_TOKEN)
    logger.info("✅ Bot initialized")


class SecretStateCleanupMiddleware(BaseMiddleware):
    """Rollback newly armed secret state when handler delivery fails."""

    async def __call__(self, handler, event: Update, data: dict):
        user_id = None
        if event.message and event.message.from_user:
            user_id = event.message.from_user.id
        elif event.callback_query:
            user_id = event.callback_query.from_user.id
        elif event.inline_query:
            user_id = event.inline_query.from_user.id

        leased_state = None
        if user_id is not None:
            leased_state = await lease_legacy_user_state(user_id)
        initial_legacy_state = user_states.get(user_id) if user_id is not None else None
        owner_task = asyncio.current_task()

        try:
            return await handler(event, data)
        except (Exception, asyncio.CancelledError):
            if user_id is not None:
                current_state = user_states.get(user_id)
                legacy_state = (current_state or {}).get("state")
                owns_current_state = bool(
                    current_state
                    and (
                        current_state is initial_legacy_state
                        or current_state.get("_owner_task") is owner_task
                    )
                )
                if legacy_state in _SECRET_LEGACY_STATES and owns_current_state:
                    await clear_legacy_user_state(
                        user_id,
                        expected_state=current_state,
                    )
            fsm_context = data.get("state")
            if isinstance(fsm_context, FSMContext):
                await _await_cancellation_safe(
                    _clear_typed_secret_state(fsm_context)
                )
            raise
        finally:
            if user_id is not None:
                current_state = user_states.get(user_id)
                if (
                    current_state is not None
                    and current_state.get("_owner_task") is owner_task
                ):
                    current_state.pop("_owner_task", None)
            if user_id is not None and leased_state is not None:
                await _await_cancellation_safe(
                    release_legacy_user_state_lease(user_id, leased_state)
                )


async def acknowledge_callback(callback: CallbackQuery) -> None:
    """Acknowledge a callback at most once for the current update."""
    if getattr(callback, "_guardbot_acknowledged", False):
        return
    setattr(callback, "_guardbot_acknowledged", True)
    await callback.answer()


async def callback_feedback(
    callback: CallbackQuery,
    text: str | None = None,
    **_answer_options,
) -> None:
    """Acknowledge once, then deliver later callback feedback as a message."""
    if not getattr(callback, "_guardbot_acknowledged", False):
        await acknowledge_callback(callback)
    if not text:
        return
    message = getattr(callback, "message", None)
    answer = getattr(message, "answer", None)
    if answer is not None:
        await answer(text)


class CallbackAcknowledgeMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Update, data: dict):
        callback = event.callback_query
        if callback is not None:
            await acknowledge_callback(callback)
            callback_message = callback.message
            callback_chat = getattr(callback_message, "chat", None)
            if (
                callback_message is None
                or getattr(callback_chat, "type", None) != ChatType.PRIVATE
            ):
                await callback_feedback(
                    callback,
                    "🔒 Персональные меню доступны только в личном чате с ботом.",
                    show_alert=True,
                )
                return None
        return await handler(event, data)


class LockCheckMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Update, data: dict):
        # Получаем пользователя
        user_id = None

        if event.message:
            user_id = event.message.from_user.id
            msg = event.message
        elif event.callback_query:
            user_id = event.callback_query.from_user.id
            msg = event.callback_query.message
        elif event.inline_query:
            user_id = event.inline_query.from_user.id
            msg = None
        else:
            return None

        if event.callback_query:
            callback_message = event.callback_query.message
            callback_chat = getattr(callback_message, "chat", None)
            if (
                callback_message is None
                or getattr(callback_chat, "type", None) != ChatType.PRIVATE
            ):
                await callback_feedback(
                    event.callback_query,
                    "🔒 Персональные меню доступны только в личном чате с ботом.",
                    show_alert=True,
                )
                return

        # During the incremental migration, a legacy flow wins over a stale
        # typed flow so two state stores cannot route the same input.
        fsm_context = data.get("state")
        deferred_fsm_clear = False
        had_legacy_state = user_id in user_states
        legacy_state = user_states.get(user_id, {}).get("state")
        if event.callback_query:
            callback_data = event.callback_query.data or ""
            starts_secret_prompt = (
                callback_data in _SECRET_PROMPT_CALLBACKS
                or callback_data.startswith(_SECRET_PROMPT_CALLBACK_PREFIXES)
            )
            callback_message = event.callback_query.message
            if (
                starts_secret_prompt
                and (
                    callback_message is None
                    or callback_message.chat.type != ChatType.PRIVATE
                )
            ):
                await callback_feedback(
                    event.callback_query,
                    "❌ Секретные данные можно вводить только в личном чате с ботом.",
                    show_alert=True,
                )
                return
            if legacy_state in _SECRET_LEGACY_STATES:
                await clear_legacy_user_state(user_id)
                legacy_state = None
        document = getattr(event.message, "document", None) if event.message else None
        document_name = (getattr(document, "file_name", None) or "").lower()
        cancel_command = None
        navigation_command = None
        current_bot_command = None
        is_foreign_bot_command = False
        message_command_text = (
            (
                getattr(event.message, "text", None)
                or getattr(event.message, "caption", None)
                or ""
            )
            if event.message
            else ""
        )
        bot_instance = data.get("bot")
        if event.message and bot_instance is not None:
            cancel_command = await _CANCEL_COMMAND_FILTER(
                event.message,
                bot_instance,
            )
            navigation_command = await _NAVIGATION_COMMAND_FILTER(
                event.message,
                bot_instance,
            )
            current_bot_command = await _ANY_COMMAND_FILTER(
                event.message,
                bot_instance,
            )
            command_parts = message_command_text.split(maxsplit=1)
            command_token = command_parts[0] if command_parts else ""
            is_foreign_bot_command = bool(
                command_token.startswith("/")
                and "@" in command_token
                and not current_bot_command
            )
        if navigation_command:
            await clear_user_workflows(
                user_id,
                fsm_context if isinstance(fsm_context, FSMContext) else None,
            )
            had_legacy_state = False
            legacy_state = None
            deferred_fsm_clear = False
            data["raw_state"] = None
        if cancel_command:
            setattr(
                event.message,
                "_guardbot_cancelled_workflow",
                {
                    "typed_state": data.get("raw_state"),
                    "legacy_state": legacy_state,
                    "had_workflow": bool(
                        data.get("raw_state") or had_legacy_state
                    ),
                },
            )
            await clear_user_workflows(
                user_id,
                fsm_context if isinstance(fsm_context, FSMContext) else None,
            )
            had_legacy_state = False
            legacy_state = None
            deferred_fsm_clear = False
            data["raw_state"] = None
        is_secret_document = bool(
            not is_foreign_bot_command
            and document_name.endswith(_SECRET_DOCUMENT_SUFFIXES)
        )
        is_secret_flow_message = bool(
            event.message
            and not is_foreign_bot_command
            and (
                legacy_state in _SECRET_LEGACY_STATES
                or data.get("raw_state") in _SECRET_FSM_STATES
            )
        )
        if event.message and (is_secret_flow_message or is_secret_document):
            if not await delete_secret_message_or_abort(event.message):
                return
            if event.message.chat.type != ChatType.PRIVATE:
                await event.message.answer(
                    "❌ Секретные данные можно вводить только в личном чате с ботом."
                )
                return
            if (
                legacy_state in _SECRET_LEGACY_STATES
                and message_command_text.startswith("/")
                and not cancel_command
            ):
                await clear_legacy_user_state(user_id)
                legacy_state = None
        if (
            had_legacy_state
            and data.get("raw_state")
            and fsm_context
            and not is_foreign_bot_command
        ):
            is_explicit_unlock_transition = bool(
                event.callback_query
                and event.callback_query.data in {"unlock_bot", "cancel_unlock"}
            )
            if is_explicit_unlock_transition:
                await fsm_context.clear()
            else:
                deferred_fsm_clear = True
            data["raw_state"] = None

        if data.get("raw_state") == UnlockFlow.waiting_password.state:
            is_password_message = bool(
                event.message
                and event.message.chat.type == ChatType.PRIVATE
                and event.message.text
                and not event.message.text.startswith("/")
            )
            is_cancel = bool(
                event.callback_query
                and event.callback_query.data == "cancel_unlock"
                and event.callback_query.message
                and event.callback_query.message.chat.type == ChatType.PRIVATE
            )
            if is_password_message or is_cancel:
                return await handler(event, data)

        # 🔥 Проверяем callback_query на разблокировку
        if event.callback_query:
            if (
                event.callback_query.data == "unlock_bot"
                and event.callback_query.message
                and event.callback_query.message.chat.type == ChatType.PRIVATE
            ):
                return await handler(event, data)

        # Только точная команда /unlock текущего бота работает при блокировке.
        bot_instance = data.get("bot")
        if event.message and bot_instance is not None:
            unlock_command = await _UNLOCK_COMMAND_FILTER(
                event.message,
                bot_instance,
            )
            set_password_command = await _SET_PASSWORD_COMMAND_FILTER(
                event.message,
                bot_instance,
            )
            set_proxy_command = await _SET_PROXY_COMMAND_FILTER(
                event.message,
                bot_instance,
            )
            set_account_proxy_command = await _SET_ACCOUNT_PROXY_COMMAND_FILTER(
                event.message,
                bot_instance,
            )
            import_mafile_command = await _IMPORT_MAFILE_COMMAND_FILTER(
                event.message,
                bot_instance,
            )
            bulk_import_command = await _BULK_IMPORT_COMMAND_FILTER(
                event.message,
                bot_instance,
            )
            login_command = await _LOGIN_COMMAND_FILTER(
                event.message,
                bot_instance,
            )
            note_command = await _NOTE_COMMAND_FILTER(
                event.message,
                bot_instance,
            )
            is_secret_command = bool(
                unlock_command
                or set_password_command
                or set_proxy_command
                or set_account_proxy_command
                or import_mafile_command
                or bulk_import_command
                or login_command
                or note_command
            )
            if is_secret_command:
                if not await delete_secret_message_or_abort(event.message):
                    return
            if (
                is_secret_command
                and event.message.chat.type != ChatType.PRIVATE
            ):
                await event.message.answer(
                    "❌ Секретные данные можно вводить только в личном чате с ботом."
                )
                return
            if unlock_command:
                return await handler(event, data)

        # Проверяем блокировку
        async with AsyncSessionLocal() as session:
            stmt = select(User).where(User.telegram_id == user_id)
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()

            if user and user.bot_password:
                # Проверяем таймаут
                if user.lock_timeout and user.last_activity:
                    timeout_seconds = user.lock_timeout * 60
                    now = datetime.now(UTC).replace(tzinfo=None)
                    elapsed = (now - user.last_activity).total_seconds()

                    if elapsed > timeout_seconds:
                        # Блокируем
                        user.last_activity = None
                        await session.commit()
                        await clear_legacy_user_state(user_id)
                        state = data.get("state")
                        if isinstance(state, FSMContext):
                            await state.clear()
                        data["raw_state"] = None
                        if event.inline_query:
                            await event.inline_query.answer(
                                [],
                                cache_time=0,
                                is_personal=True,
                            )
                            return
                        if event.callback_query:
                            await callback_feedback(event.callback_query)
                        await msg.answer(
                            "🔒 <b>БОТ ЗАБЛОКИРОВАН</b>\n\n"
                            "Сработала автоблокировка по бездействию.\n"
                            "<code>/unlock [пароль]</code> — разблокировать",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(
                                inline_keyboard=[
                                    [InlineKeyboardButton(text="🔓 Разблокировать", callback_data="unlock_bot")]
                                ]
                            )
                        )
                        return

                # Проверяем ручную блокировку
                if user.last_activity is None:
                    if event.inline_query:
                        await event.inline_query.answer(
                            [],
                            cache_time=0,
                            is_personal=True,
                        )
                        return
                    if event.callback_query:
                        await callback_feedback(event.callback_query)
                    await msg.answer(
                        "🔒 <b>БОТ ЗАБЛОКИРОВАН</b>\n\n"
                        "<code>/unlock [пароль]</code> — разблокировать",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(text="🔓 Разблокировать", callback_data="unlock_bot")]
                            ]
                        )
                    )
                    return

                # Обновляем время активности
                user.last_activity = datetime.now(UTC).replace(tzinfo=None)
                await session.commit()

        if is_foreign_bot_command:
            return None
        if (
            event.callback_query
            and data.get("raw_state")
            and isinstance(fsm_context, FSMContext)
        ):
            # Any local callback leaves the current text-entry screen. Flow
            # starters clear and re-arm their own state inside the handler,
            # while ordinary navigation must not leave stale secret input
            # active behind the newly rendered destination.
            await fsm_context.clear()
            data["raw_state"] = None
        if deferred_fsm_clear and isinstance(fsm_context, FSMContext):
            await fsm_context.clear()
        return await handler(event, data)

dp = Dispatcher(
    storage=MemoryStorage(),
    fsm_strategy=FSMStrategy.GLOBAL_USER,
    events_isolation=EvictingEventIsolation(),
    disable_fsm=True,
)
dp.update.outer_middleware(CallbackAcknowledgeMiddleware())
dp.update.outer_middleware(dp.fsm)
dp.update.outer_middleware(LockCheckMiddleware())
dp.update.outer_middleware(SecretStateCleanupMiddleware())

user_states = {}

def calculate_steam_price(price_float: float, currency_id: int) -> int:
    """
    Рассчитывает цену для отправки в Steam с учётом комиссии и минимальной комиссии

    Args:
        price_float: цена в валюте (например, 100.0)
        currency_id: ID валюты из STEAM_CURRENCIES

    Returns:
        int: цена в копейках/центах для отправки в Steam
    """
    import math

    # Получаем информацию о валюте
    currency_info = STEAM_CURRENCIES.get(currency_id, {})
    min_commission = currency_info.get('min_commission')  # может быть None

    # 1. Вычисляем цену после процентной комиссии (3/23)
    price_after_percent = price_float * 20 / 23

    # 2. Если минимальная комиссия задана — проверяем
    if min_commission is not None:
        # Вычисляем сумму комиссии в копейках/центах
        commission_amount = (price_float * 100) - (price_after_percent * 100)

        if commission_amount < min_commission:
            # Комиссия меньше минимальной — вычитаем минимальную
            price_floored = math.floor((price_float * 100 - min_commission)) / 100
            logger.info(f"💰 Min commission applied: {min_commission/100:.2f} (regular would be {commission_amount/100:.2f})")
        else:
            # Обычная комиссия 3/23 с округлением вниз
            price_floored = math.floor(price_after_percent * 100) / 100
    else:
        # Минимальная комиссия не задана — используем обычную 3/23
        price_floored = math.floor(price_after_percent * 100) / 100

    # 3. Конвертируем в копейки/центы
    price_to_send = int(price_floored * 100)

    logger.info(f"💰 Price calculation: {price_float} -> after commission: {price_floored} -> send: {price_to_send}")

    return price_to_send


def get_inventory_context_id(app_id: int) -> int:
    """Return the Steam inventory context used by a supported app."""
    return 6 if app_id == 753 else 2


@dp.message(CustomPriceFlow.waiting_price, F.text, ~F.text.startswith("/"))
async def process_custom_price(message: Message, state: FSMContext):
    """Sell an owned inventory item using a one-shot custom price."""
    if message.chat.type != ChatType.PRIVATE:
        await message.answer(
            "❌ Цену можно вводить только в личном чате с ботом."
        )
        return

    raw_price = (message.text or "").strip()
    if not re.fullmatch(r"\d{1,8}(?:[.,]\d{1,2})?", raw_price):
        await message.answer("❌ Введите корректное число (например: 8.70)")
        return

    try:
        price = Decimal(raw_price.replace(",", "."))
    except InvalidOperation:
        await message.answer("❌ Введите корректное число (например: 8.70)")
        return
    if not price.is_finite() or price < Decimal("0.01"):
        await message.answer("❌ Цена должна быть не меньше 0.01")
        return
    if price > Decimal("21474836.47"):
        await message.answer("❌ Цена слишком большая")
        return
    price_float = float(price)
    if not math.isfinite(price_float):
        await message.answer("❌ Цена слишком большая")
        return

    state_data = await state.get_data()
    mafile_id = state_data.get("mafile_id")
    app_id = state_data.get("app_id")
    assetid = state_data.get("assetid")
    context_id = state_data.get("context_id")
    valid_state = (
        type(mafile_id) is int
        and mafile_id > 0
        and type(app_id) is int
        and app_id > 0
        and isinstance(assetid, str)
        and bool(re.fullmatch(r"[1-9]\d*", assetid))
        and type(context_id) is int
        and context_id > 0
    )
    if not valid_state:
        await clear_user_workflows(message.from_user.id, state)
        await message.answer("❌ Не удалось определить предмет")
        return

    client = None
    price_text = format(price.quantize(Decimal("0.01")), "f")
    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, message.from_user.id)
        if not mafile:
            await clear_user_workflows(message.from_user.id, state)
            await message.answer("❌ Аккаунт не найден")
            return

        currency = mafile.currency_id or 18
        price_with_commission = calculate_steam_price(price_float, currency)
        if price_with_commission <= 0:
            await message.answer("❌ Цена слишком низкая после комиссии")
            return
        if price_with_commission > 2_147_483_647:
            await message.answer("❌ Цена слишком большая")
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

    try:
        status_msg = await message.answer(
            f"💰 <b>Продаю предмет за {price_text}...</b>\n"
            f"<i>Цена после комиссии (3/23): {price_with_commission / 100:.2f}</i>",
            parse_mode="HTML",
        )
        await clear_user_workflows(message.from_user.id, state)

        result = await client.sell_item_with_recovery(
            app_id,
            context_id,
            assetid,
            1,
            price_with_commission,
        )
        if result.get("success"):
            if result.get("needs_mobile_confirmation"):
                text = (
                    "✅ <b>Предмет выставлен на продажу!</b>\n\n"
                    f"💵 <b>Цена:</b> {price_text}\n\n"
                    "⚠️ <b>Требуется подтверждение в Steam Guard</b>"
                )
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="✅ ПЕРЕЙТИ К ПОДТВЕРЖДЕНИЯМ",
                            callback_data=f"confirmations_menu_{mafile_id}",
                        )],
                        [InlineKeyboardButton(
                            text="🎒 В меню инвентаря",
                            callback_data=f"inventory_menu_{mafile_id}",
                        )],
                    ]
                )
            else:
                text = (
                    "✅ <b>Предмет успешно выставлен на продажу за "
                    f"{price_text}!</b>"
                )
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="🎒 В меню инвентаря",
                            callback_data=f"inventory_menu_{mafile_id}",
                        )]
                    ]
                )
            await status_msg.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            error = escape(str(result.get("error", "Неизвестная ошибка"))[:300])
            await status_msg.edit_text(
                "❌ <b>Не удалось продать предмет</b>\n\n"
                f"<code>{error}</code>",
                parse_mode="HTML",
            )
    finally:
        await close_owned_resource(client)

def _parse_sell_item_callback_data(
    callback_data: str | None,
) -> tuple[str, int, int, str] | None:
    match = re.fullmatch(
        r"sell_item_(min|median|custom)_"
        r"([1-9][0-9]{0,9})_([1-9][0-9]{0,9})_([1-9][0-9]{0,19})",
        callback_data or "",
    )
    if match is None:
        return None

    sell_type, raw_mafile_id, raw_app_id, assetid = match.groups()
    mafile_id = int(raw_mafile_id)
    app_id = int(raw_app_id)
    if (
        mafile_id > 2_147_483_647
        or app_id > 2_147_483_647
        or int(assetid) > 18_446_744_073_709_551_615
    ):
        return None
    return sell_type, mafile_id, app_id, assetid


@dp.callback_query(F.data.startswith("sell_item_"))
async def callback_sell_item(callback: CallbackQuery, state: FSMContext):
    """Продажа предмета по минимальной или средней цене"""
    telegram_id = callback.from_user.id
    await clear_user_workflows(telegram_id, state)

    parsed = _parse_sell_item_callback_data(callback.data)
    if parsed is None:
        await callback_feedback(callback, "❌ Некорректное действие")
        return

    sell_type, mafile_id, app_id, assetid = parsed
    context_id = get_inventory_context_id(app_id)

    # Проверяем наличие прокси
    has_proxy, _ = await check_proxy_available(telegram_id, for_steam_requests=True)
    if not has_proxy:
        await callback_feedback(callback, "❌ Нет доступного прокси!", show_alert=True)
        return

    if sell_type == "custom":
        async with AsyncSessionLocal() as session:
            mafile = await get_owned_mafile(session, mafile_id, telegram_id)
            if not mafile:
                await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
                return

        try:
            await state.set_state(CustomPriceFlow.waiting_price)
            await state.set_data({
                "mafile_id": mafile_id,
                "app_id": app_id,
                "assetid": assetid,
                "context_id": context_id,
            })
            await callback.message.edit_text(
                "💰 <b>Введите цену продажи:</b>\n\n"
                "Отправьте число — цену в вашей валюте (например: <code>8.70</code>)\n\n"
                "<i>Комиссия Steam (13%) будет учтена автоматически.</i>\n\n"
                "Для отмены нажмите кнопку ниже:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="❌ Отмена",
                            callback_data=f"inventory_menu_{mafile_id}",
                        )]
                    ]
                ),
            )
        except (Exception, asyncio.CancelledError):
            await clear_user_workflows(telegram_id, state)
            raise
        await callback_feedback(callback)
        return

    # Для min и median - получаем цену и продаём
    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        if not mafile.access_token:
            await callback_feedback(callback, "Нет активной сессии", show_alert=True)
            return

        # Получаем страну и валюту
        country = mafile.country or "UA"
        currency = mafile.currency_id or 18

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            # Получаем инвентарь чтобы найти market_hash_name
            data = await client.get_inventory(app_id, context_id)
            if not data:
                await callback_feedback(callback, "Не удалось загрузить инвентарь", show_alert=True)
                return

            items = process_inventory_items_universal(data, app_id)
            item = next((i for i in items if i['assetid'] == assetid), None)

            if not item or not item.get('market_hash_name'):
                await callback_feedback(callback, "Предмет не найден", show_alert=True)
                return

            # Получаем цену
            await client._ensure_session()
            price_data = await get_item_price(client, app_id, item['market_hash_name'], country, currency)

            if not price_data:
                await callback_feedback(callback, "Не удалось получить цену", show_alert=True)
                return

            # Определяем цену для продажи
            if sell_type == "min":
                price_str = price_data['lowest_price']
            else:  # median
                price_str = price_data['median_price']

            # Парсим цену (убираем символ валюты и преобразуем в число)
            # Извлекаем число (может быть с запятой или точкой)
            price_match = re.search(r'[\d.,]+', price_str.replace(' ', ''))
            if not price_match:
                await callback_feedback(callback, "Не удалось распарсить цену", show_alert=True)
                return

            price_float = float(price_match.group().replace(',', '.'))

            # Конвертируем в копейки/центы и вычитаем комиссию 3/23%
            price_with_commission = calculate_steam_price(price_float, currency)

            if price_with_commission <= 0:
                await callback_feedback(callback, "Цена слишком низкая после комиссии", show_alert=True)
                return

            type_text = "минимальной" if sell_type == "min" else "средней"
            safe_item_name = escape(str(item["name"]))
            safe_price_str = escape(str(price_str))

            await callback_feedback(callback, f"Продаю по {type_text} цене: {price_float} -> {price_with_commission/100:.2f} (после комиссии)...")

            status_msg = await callback.message.edit_text(
                f"💰 <b>Продаю предмет по {type_text} цене...</b>\n\n"
                f"📛 <b>Предмет:</b> {safe_item_name}\n"
                f"💵 <b>Цена:</b> {safe_price_str}\n"
                f"📊 <b>Цена после комиссии (13%):</b> {price_with_commission/100:.2f}\n\n"
                f"<i>Отправляю запрос...</i>",
                parse_mode="HTML"
            )

            # Продаём
            result = await client.sell_item_with_recovery(
                app_id,
                context_id,
                assetid,
                1,
                price_with_commission,
            )
            # Показываем статус восстановления сессии
            await show_recovery_status(callback, client)

            if result.get('needauth'):
                await status_msg.edit_text(
                    f"❌ <b>Требуется вход в аккаунт!</b>\n\n"
                    f"Сессия устарела. Выполните вход заново.\n\n"
                    f"<i>Выберите действие:</i>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(text="🔑 Войти по паролю", callback_data=f"action_login_{mafile_id}"),
                                InlineKeyboardButton(text="🔄 Войти по токену", callback_data=f"try_refresh_{mafile_id}")
                            ],
                            [InlineKeyboardButton(
                                text="◀️ К инвентарю",
                                callback_data=f"inventory_menu_{mafile_id}"
                            )]
                        ]
                    )
                )
                return

            if result.get('success'):
                if result.get('needs_mobile_confirmation'):
                    text = (
                        f"✅ <b>Предмет выставлен на продажу!</b>\n\n"
                        f"📛 <b>Предмет:</b> {safe_item_name}\n"
                        f"💵 <b>Цена:</b> {safe_price_str}\n\n"
                        f"⚠️ <b>Требуется подтверждение в Steam Guard</b>\n\n"
                        f"<i>Нажмите кнопку ниже, чтобы перейти к подтверждениям:</i>"
                    )

                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="✅ ПЕРЕЙТИ К ПОДТВЕРЖДЕНИЯМ",
                                callback_data=f"confirmations_menu_{mafile_id}"
                            )],
                            [InlineKeyboardButton(
                                text="🎒 В меню инвентаря",
                                callback_data=f"inventory_menu_{mafile_id}"
                            )]
                        ]
                    )

                elif result.get('needs_email_confirmation'):
                    email_domain = result.get('email_domain', 'неизвестный домен')
                    safe_email_domain = escape(str(email_domain))
                    text = (
                        f"✅ <b>Предмет выставлен на продажу!</b>\n\n"
                        f"📛 <b>Предмет:</b> {safe_item_name}\n"
                        f"💵 <b>Цена:</b> {safe_price_str}\n\n"
                        f"📧 <b>Требуется подтверждение по email</b>\n"
                        f"📨 Домен почты: <code>{safe_email_domain}</code>\n\n"
                        f"<i>Проверьте почту и подтвердите продажу.</i>"
                    )

                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="🎒 В меню инвентаря",
                                callback_data=f"inventory_menu_{mafile_id}"
                            )]
                        ]
                    )
                else:
                    text = (
                        f"✅ <b>Предмет успешно выставлен на продажу!</b>\n\n"
                        f"📛 <b>Предмет:</b> {safe_item_name}\n"
                        f"💵 <b>Цена:</b> {safe_price_str}\n\n"
                        f"📦 Предмет появится на торговой площадке в ближайшее время."
                    )

                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="🎒 В меню инвентаря",
                                callback_data=f"inventory_menu_{mafile_id}"
                            )]
                        ]
                    )

                await status_msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
            else:
                error = result.get('error', 'Неизвестная ошибка')
                await status_msg.edit_text(
                    f"❌ <b>Не удалось продать предмет</b>\n\n"
                    f"<code>{escape(str(error)[:300])}</code>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="🔄 Попробовать снова",
                                callback_data=f"sell_item_{sell_type}_{mafile_id}_{app_id}_{assetid}"
                            )],
                            [InlineKeyboardButton(
                                text="🎒 В меню инвентаря",
                                callback_data=f"inventory_menu_{mafile_id}"
                            )]
                        ]
                    )
                )

        except Exception as e:
            logger.error(f"Error in sell item callback: {e}", exc_info=True)
            await callback_feedback(callback, f"Ошибка: {str(e)[:100]}", show_alert=True)
        finally:
            await close_owned_resource(client)

async def get_item_price(client: AsyncSteamMobile, app_id: int, market_hash_name: str, country: str = "UA", currency: int = 18) -> dict | None:
    """
    Получает цену предмета с Steam Market

    Args:
        client: Steam-клиент с обязательным proxy-only transport
        app_id: ID игры (730=CS2)
        market_hash_name: полное имя предмета для маркета
        country: код страны (UA, US, etc.)
        currency: ID валюты (18=UAH)

    Returns:
        dict с ключами lowest_price, median_price, volume или None
    """
    url = "https://steamcommunity.com/market/priceoverview/"
    params = {
        'country': country,
        'currency': currency,
        'appid': app_id,
        'market_hash_name': market_hash_name
    }

    headers = {
        'User-Agent': USER_AGENT_MOBILE,
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
    }

    try:
        resp = await client._request('GET', url, params=params, headers=headers)
        if resp.status != 200:
            logger.warning(f"Price overview HTTP {resp.status} for '{market_hash_name}'")
            return None

        data = await resp.json()
        if data.get('success'):
            logger.info(f"💰 Price for '{market_hash_name}': lowest={data.get('lowest_price')}, median={data.get('median_price')}")
            return {
                'lowest_price': data.get('lowest_price', 'N/A'),
                'median_price': data.get('median_price', 'N/A'),
                'volume': data.get('volume', '0')
            }

        logger.warning(f"❌ Price overview failed for '{market_hash_name}' (app {app_id}, country {country}, currency {currency})")
        return None

    except ProxyRequiredError:
        raise
    except Exception as e:
        logger.error(f"Error getting price for {market_hash_name}: {e}")
        return None

def process_inventory_items_universal(data: dict, app_id: int) -> list:
    """Универсальная обработка предметов инвентаря для разных игр"""
    import re

    assets = data.get('assets', [])
    descriptions = data.get('descriptions', [])
    asset_properties_list = data.get('asset_properties', [])

    # Словарь описаний по ключу classid_instanceid
    desc_dict = {}
    for desc in descriptions:
        classid = desc.get('classid')
        instanceid = desc.get('instanceid')
        if classid is not None and instanceid is not None:
            key = f"{classid}_{instanceid}"
            desc_dict[key] = desc

    # Словарь свойств по assetid
    properties_dict = {}
    if asset_properties_list:
        for prop in asset_properties_list:
            assetid = str(prop.get('assetid', ''))
            if assetid:
                properties_dict[assetid] = prop

    rarity_to_emoji = {
        'Covert': '🔴',
        'Extraordinary': '🔴',
        'Classified': '🟣',
        'Restricted': '🟣',
        'Remarkable': '🟣',
        'Mil-Spec Grade': '🔵',
        'High Grade': '🔵',
        'Industrial Grade': '🔵',
        'Consumer Grade': '⚪',
        'Base Grade': '⚪',
        'Stock': '⚪',
    }

    items = []
    for asset in assets:
        assetid = str(asset.get('assetid', ''))
        classid = asset.get('classid')
        instanceid = asset.get('instanceid')
        amount = int(asset.get('amount', 1))

        desc_key = f"{classid}_{instanceid}"
        desc = desc_dict.get(desc_key, {})

        name = desc.get('market_name') or desc.get('name') or 'Unknown Item'
        market_hash_name = desc.get('market_hash_name', '')

        icon_url = ''
        if desc.get('icon_url'):
            icon_url = f"https://community.akamai.steamstatic.com/economy/image/{desc['icon_url']}"

        rarity_name = ''
        rarity_emoji = '⚪'
        rarity_color = '#b0c3d9'

        for tag in desc.get('tags', []):
            if tag.get('category') == 'Rarity':
                rarity_name = tag.get('localized_tag_name', '')
                rarity_emoji = rarity_to_emoji.get(rarity_name, '⚪')
                color = tag.get('color', '')
                if color:
                    rarity_color = f"#{color}" if not color.startswith('#') else color
                break

        item_type = ''
        for tag in desc.get('tags', []):
            if tag.get('category') == 'Type':
                item_type = tag.get('localized_tag_name', '')
                break

        item = {
            'assetid': assetid,
            'classid': classid,
            'instanceid': instanceid,
            'app_id': app_id,
            'name': name,
            'market_hash_name': market_hash_name,
            'icon_url': icon_url,
            'rarity': rarity_name,
            'rarity_emoji': rarity_emoji,
            'rarity_color': rarity_color,
            'item_type': item_type,
            'tradable': desc.get('tradable', 0) == 1,
            'marketable': desc.get('marketable', 0) == 1,
            'amount': amount,
            'exterior': '',
            'float': None,
            'pattern': None,
            'paint_seed': None,
            'is_stattrak': False,
            'stattrak_count': None,
            'collection': '',
            'stickers': [],
            'hero': '',
            'quality': '',
            'name_tag': None,
            'inspect_link': None,  # 🔥 Новое поле для ссылки на осмотр
        }

        # ========== CS2 / TF2 специфичные поля ==========
        if app_id in [730, 440]:
            # Exterior
            for tag in desc.get('tags', []):
                if tag.get('category') == 'Exterior':
                    item['exterior'] = tag.get('localized_tag_name', '')
                    break

            # StatTrak
            for tag in desc.get('tags', []):
                if tag.get('category') == 'Quality' and 'StatTrak' in tag.get('localized_tag_name', ''):
                    item['is_stattrak'] = True
                    break

            # StatTrak счетчик
            for d in desc.get('descriptions', []):
                value = d.get('value', '')
                if 'StatTrak' in value and ('Kills' in value or 'Kill' in value):
                    match = re.search(r'(\d[\d,]*)', value)
                    if match:
                        try:
                            item['stattrak_count'] = int(match.group(1).replace(',', ''))
                        except:
                            pass
                    break

            # Коллекция
            for tag in desc.get('tags', []):
                if tag.get('category') == 'ItemSet':
                    item['collection'] = tag.get('localized_tag_name', '')
                    break

            # Float, Pattern, Paint Seed, Name Tag И Inspect Link из asset_properties
            asset_props = properties_dict.get(assetid)
            if asset_props:
                props_list = asset_props.get('asset_properties', [])

                for prop in props_list:
                    prop_id = prop.get('propertyid')
                    if prop_id == 1:  # Pattern Template
                        item['pattern'] = prop.get('int_value')
                    elif prop_id == 2:  # Wear Rating (Float)
                        try:
                            item['float'] = float(prop.get('float_value', 0))
                        except (ValueError, TypeError):
                            pass
                    elif prop_id == 3:  # Paint Seed
                        item['paint_seed'] = prop.get('int_value')
                    elif prop_id == 5:  # Name Tag
                        name_tag_raw = prop.get('string_value', '')
                        if name_tag_raw:
                            try:
                                item['name_tag'] = bytes.fromhex(name_tag_raw).decode('utf-8', errors='replace')
                            except:
                                item['name_tag'] = name_tag_raw
                    elif prop_id == 6:  # Item Certificate (ссылка на осмотр)
                        certificate = prop.get('string_value', '')
                        if certificate:
                            item['inspect_link'] = f"steam://rungame/730/{app_id}/+csgo_econ_action_preview%20S{certificate}"

            # 🔥 ПАРСИМ НАКЛЕЙКИ ИЗ sticker_info В ОПИСАНИИ
            sticker_info_html = None
            for d in desc.get('descriptions', []):
                if d.get('name') == 'sticker_info':
                    sticker_info_html = d.get('value', '')
                    break

            if sticker_info_html:
                # Ищем все title атрибуты в img тегах
                sticker_titles = re.findall(r'title="([^"]+)"', sticker_info_html)

                for title in sticker_titles:
                    if title.startswith('Sticker:'):
                        sticker_name = title.replace('Sticker:', '').strip()
                        item['stickers'].append({
                            'name': sticker_name,
                            'icon_url': '',
                            'wear': None
                        })

            # 🔥 Парсим износ из asset_accessories
            # Важно: порядок аксессуаров соответствует порядку стикеров в sticker_info
            if asset_props:
                accessories = asset_props.get('asset_accessories', [])
                # Берем только те аксессуары, у которых есть parent_relationship_properties с propertyid=4
                sticker_wears = []
                for accessory in accessories:
                    for rel_prop in accessory.get('parent_relationship_properties', []):
                        if rel_prop.get('propertyid') == 4:
                            try:
                                wear = float(rel_prop.get('float_value', 0))
                                sticker_wears.append(wear)
                            except (ValueError, TypeError):
                                sticker_wears.append(0)
                            break

                # Применяем износ к стикерам в том же порядке
                for i, wear in enumerate(sticker_wears):
                    if i < len(item['stickers']):
                        item['stickers'][i]['wear'] = wear

            # Также ищем иконки для стикеров из описаний аксессуаров
            if asset_props:
                accessories = asset_props.get('asset_accessories', [])
                # Строим словарь classid -> description для аксессуаров
                acc_classids = set()
                for acc in accessories:
                    acc_classid = str(acc.get('classid', ''))
                    if acc_classid:
                        acc_classids.add(acc_classid)

                acc_desc_dict = {}
                for d in descriptions:
                    if str(d.get('classid', '')) in acc_classids:
                        acc_desc_dict[str(d.get('classid', ''))] = d

                for i, accessory in enumerate(accessories):
                    acc_classid = str(accessory.get('classid', ''))
                    acc_desc = acc_desc_dict.get(acc_classid)
                    if acc_desc and i < len(item['stickers']):
                        icon = acc_desc.get('icon_url', '')
                        if icon:
                            item['stickers'][i]['icon_url'] = f"https://community.akamai.steamstatic.com/economy/image/{icon}"

        # ========== Dota 2 специфичные поля ==========
        elif app_id == 570:
            for tag in desc.get('tags', []):
                if tag.get('category') == 'Quality':
                    item['quality'] = tag.get('localized_tag_name', '')
                    break
            for tag in desc.get('tags', []):
                if tag.get('category') == 'Hero':
                    item['hero'] = tag.get('localized_tag_name', '')
                    break

        # ========== Steam ==========
        elif app_id == 753:
            item['item_type'] = item_type or desc.get('type', '')

        items.append(item)

    return items


def _trade_accept_callback(mafile_id, tradeofferid) -> str | None:
    values = []
    for raw_value, maximum in (
        (mafile_id, 2_147_483_647),
        (tradeofferid, 18_446_744_073_709_551_615),
    ):
        if type(raw_value) is int:
            value = raw_value
        elif (
            isinstance(raw_value, str)
            and raw_value
            and raw_value.isascii()
            and raw_value.isdecimal()
        ):
            value = int(raw_value)
        else:
            return None
        if not 0 < value <= maximum:
            return None
        values.append(str(value))
    payload = f"trade_accept_{values[0]}_{values[1]}"
    return payload if len(payload.encode("utf-8")) <= 64 else None


def _trade_notification_callback(
    action: str,
    mafile_id,
    tradeofferid,
) -> str | None:
    if action not in {"detail", "decline"}:
        return None
    values = []
    for raw_value, maximum in (
        (mafile_id, 2_147_483_647),
        (tradeofferid, 18_446_744_073_709_551_615),
    ):
        if type(raw_value) is int:
            value = raw_value
        elif (
            isinstance(raw_value, str)
            and raw_value
            and raw_value.isascii()
            and raw_value.isdecimal()
        ):
            value = int(raw_value)
        else:
            return None
        if not 0 < value <= maximum:
            return None
        values.append(str(value))
    if action == "detail":
        payload = f"trade_detail_full_{values[0]}_{values[1]}_incoming"
    else:
        payload = f"trade_decline_{values[0]}_{values[1]}"
    return payload if len(payload.encode("utf-8")) <= 64 else None


# Словарь для хранения запущенных задач проверки трейдов
trade_check_tasks: dict[int, asyncio.Task] = {}

async def check_new_trades_for_account(mafile_id: int):
    """Фоновая задача для проверки новых трейдов"""
    logger.info(f"Запущена проверка трейдов для mafile_id={mafile_id}")

    while True:
        try:
            async with AsyncSessionLocal() as session:
                mafile = await session.get(Mafile, mafile_id)

                # Проверяем, что аккаунт существует и уведомления включены
                if not mafile or not mafile.trade_notifications or not mafile.trade_notify_interval:
                    logger.info(f"Проверка трейдов для {mafile_id} остановлена: уведомления выключены")
                    break

                # Проверяем, что есть токен для запросов
                if not mafile.access_token:
                    logger.warning(f"Нет токена для проверки трейдов {mafile.account_name}")
                    await asyncio.sleep(mafile.trade_notify_interval * 60)
                    continue

                # Проверяем прокси
                has_proxy, _ = await check_proxy_available(mafile.telegram_id, for_steam_requests=True)
                if not has_proxy:
                    logger.warning(f"Нет прокси для проверки трейдов {mafile.account_name}")
                    await asyncio.sleep(mafile.trade_notify_interval * 60)
                    continue

                client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

                try:
                    # Получаем активные входящие трейды
                    html_text = await client.get_trade_offers_page(get_received=True, active_only=False, history=False)
                    offers = await client.parse_trade_offers_list(html_text)

                    # Фильтруем только активные входящие
                    active_offers = [o for o in offers if o['status'] == 'active']

                    # Получаем список известных ID
                    known_ids = set(mafile.known_trade_ids or [])
                    current_ids = set(o['tradeofferid'] for o in active_offers)

                    # Находим новые трейды
                    new_ids = current_ids - known_ids

                    if new_ids:
                        logger.info(f"Найдено {len(new_ids)} новых трейдов для {mafile.account_name}")

                        # Отправляем уведомления
                        new_offers = [o for o in active_offers if o['tradeofferid'] in new_ids]

                        for offer in new_offers:
                            tradeofferid = offer['tradeofferid']
                            partner_name = str(offer['partner_name'] or 'Unknown')
                            partner_id = offer['partner_id']
                            your_items = offer['your_items_count']
                            their_items = offer['their_items_count']
                            message_text = str(offer['message'] or 'Отсутствует')

                            # Формируем ссылку на трейд
                            trade_url = f"https://steamcommunity.com/tradeoffer/{tradeofferid}/"

                            # Формируем ссылку на профиль и ID для отображения
                            if partner_id:
                                partner_id = str(partner_id)
                                if partner_id.startswith('https://steamcommunity.com/'):
                                    profile_url = partner_id
                                    profile_display = partner_id.rstrip('/').split('/')[-1]
                                else:
                                    profile_url = f"https://steamcommunity.com/profiles/{partner_id}"
                                    profile_display = partner_id
                            else:
                                profile_url = trade_url
                                profile_display = "неизвестно"

                            safe_tradeofferid = escape(str(tradeofferid))
                            safe_account_name = escape(str(mafile.account_name))
                            safe_partner_name = escape(partner_name)
                            safe_profile_url = escape(profile_url, quote=True)
                            safe_profile_display = escape(str(profile_display))
                            safe_message_text = escape(message_text)
                            accept_callback = _trade_accept_callback(
                                mafile_id,
                                tradeofferid,
                            )
                            detail_callback = _trade_notification_callback(
                                "detail", mafile_id, tradeofferid
                            )
                            decline_callback = _trade_notification_callback(
                                "decline", mafile_id, tradeofferid
                            )

                            text = (
                                f"🚨 <b>ПРИШЕЛ НОВЫЙ ТРЕЙД!</b>\n\n"
                                f"⏳ <b>Трейд <a href='{trade_url}'>№{safe_tradeofferid}</a></b>\n"
                                f"📱 <b>На аккаунт:</b> <code>{safe_account_name}</code>\n"
                                f"👤 <b>От партнера:</b> {safe_partner_name} (<a href='{safe_profile_url}'>{safe_profile_display}</a>)\n"
                                f"📦 <b>Предметов от вас:</b> {escape(str(your_items))}\n"
                                f"📦 <b>Предметов от партнера:</b> {escape(str(their_items))}\n"
                                f"💬 <b>Комментарий:</b> <i>\"{safe_message_text}\"</i>"
                            )

                            # 🔥 ИСПРАВЛЕННАЯ КЛАВИАТУРА
                            keyboard_rows = []
                            if detail_callback is not None:
                                keyboard_rows.append([InlineKeyboardButton(
                                    text="👁️ Посмотреть трейд",
                                    callback_data=detail_callback,
                                )])
                            if (
                                accept_callback is not None
                                and decline_callback is not None
                            ):
                                keyboard_rows.append([
                                        InlineKeyboardButton(
                                            text="✅ Принять",
                                            callback_data=accept_callback,
                                        ),
                                        InlineKeyboardButton(
                                            text="❌ Отклонить",
                                            callback_data=decline_callback,
                                        )
                                ])
                            keyboard_rows.append([InlineKeyboardButton(
                                        text="📋 Все трейды",
                                        callback_data=f"trades_incoming_active_{mafile_id}_1"
                            )])
                            keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

                            # Отправляем уведомление
                            try:
                                await bot.send_message(
                                    chat_id=mafile.telegram_id,
                                    text=text,
                                    parse_mode="HTML",
                                    reply_markup=keyboard,
                                    disable_web_page_preview=True
                                )
                                logger.info(f"Отправлено уведомление о трейде {tradeofferid} для {mafile.account_name}")
                            except Exception as e:
                                logger.error(f"Ошибка отправки уведомления: {e}")

                    # Обновляем список известных ID
                    mafile.known_trade_ids = list(current_ids)
                    mafile.last_trade_check = datetime.utcnow()
                    await session.commit()

                    logger.info(f"Проверка трейдов для {mafile.account_name}: активно {len(active_offers)}, новых {len(new_ids)}")

                except Exception as e:
                    logger.error(f"Ошибка при проверке трейдов для {mafile.account_name}: {e}", exc_info=True)
                finally:
                    await client.close()

            # Ждем указанный интервал
            interval = mafile.trade_notify_interval if mafile else 5
            logger.info(f"Следующая проверка трейдов для {mafile.account_name} через {interval} мин")
            await asyncio.sleep(interval * 60)

        except asyncio.CancelledError:
            logger.info(f"Проверка трейдов для {mafile_id} отменена")
            break
        except Exception as e:
            logger.error(f"Критическая ошибка в проверке трейдов для {mafile_id}: {e}", exc_info=True)
            await asyncio.sleep(60)  # При критической ошибке ждем минуту и пробуем снова

def start_trade_check_task(mafile_id: int):
    """Запускает задачу проверки трейдов"""
    if mafile_id in trade_check_tasks:
        # Останавливаем старую задачу
        trade_check_tasks[mafile_id].cancel()

    task = asyncio.create_task(check_new_trades_for_account(mafile_id))
    trade_check_tasks[mafile_id] = task
    logger.info(f"Задача проверки трейдов для {mafile_id} создана")


def stop_trade_check_task(mafile_id: int):
    """Останавливает задачу проверки трейдов"""
    task = trade_check_tasks.pop(mafile_id, None)
    if task:
        task.cancel()
        logger.info(f"Задача проверки трейдов для {mafile_id} остановлена")


async def init_trade_check_tasks():
    """Инициализация задач проверки трейдов при запуске бота"""
    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.trade_notifications == True)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        for mafile in mafiles:
            start_trade_check_task(mafile.id)

        logger.info(f"Запущено {len(mafiles)} задач проверки трейдов")


async def check_proxy_available(telegram_id: int, for_steam_requests: bool = True) -> tuple[bool, Optional[str]]:
    """
    Проверяет, есть ли у пользователя хоть какой-то прокси.
    Возвращает (True, proxy) если прокси есть, иначе (False, None)
    """
    logger.info(f"check_proxy_available called with telegram_id={telegram_id}, type={type(telegram_id)}")

    if not for_steam_requests:
        return True, None

    async with AsyncSessionLocal() as session:
        # Проверяем общий прокси
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        logger.info(
            "User found: %s, general_proxy configured: %s",
            user is not None,
            bool(user and user.general_proxy),
        )

        if user and user.general_proxy:
            proxy_decrypted = decrypt_value(user.general_proxy)
            logger.info(f"General proxy type: {'SOCKS' if proxy_decrypted.startswith(('socks4://', 'socks5://')) else 'HTTP'}")
            return True, user.general_proxy

        # Проверяем, есть ли аккаунты с уникальными прокси
        stmt = select(Mafile).where(
            Mafile.telegram_id == telegram_id,
            Mafile.unique_proxy == True,
            Mafile.proxy.isnot(None)
        )
        result = await session.execute(stmt)
        mafile_with_proxy = result.scalar_one_or_none()

        logger.info(f"Unique proxy found: {mafile_with_proxy is not None}")

        if mafile_with_proxy:
            return True, None

        return False, None

def require_proxy(func):
    """Декоратор для проверки наличия прокси перед выполнением команды"""
    async def wrapper(message: Message, *args, **kwargs):
        if message.chat.type != ChatType.PRIVATE:
            with suppress(TelegramAPIError):
                await message.delete()
            await message.answer(
                "🔒 Эта Steam-команда доступна только в личном чате с ботом."
            )
            return

        telegram_id = message.from_user.id

        has_proxy, _ = await check_proxy_available(telegram_id, for_steam_requests=True)

        if not has_proxy:
            await message.answer(
                "❌ <b>Нет доступного прокси!</b>\n\n"
                "Для выполнения этого действия необходим прокси.\n\n"
                "<b>Настройте прокси:</b>\n"
                "<code>/set_proxy http://user:pass@host:port</code> - общий прокси\n"
                "<code>/set_account_proxy [acc] [url]</code> - уникальный прокси\n\n"
                "<i>Без прокси вы можете только получать Steam Guard коды.</i>",
                parse_mode="HTML"
            )
            return

        return await func(message, *args, **kwargs)
    return wrapper

async def show_recovery_status(callback: CallbackQuery, client: AsyncSteamMobile):
    """Показывает всплывашки о статусе восстановления сессии"""
    if hasattr(client, '_recovery_attempted') and client._recovery_attempted:
        # Сначала показываем что пробовали
        await callback_feedback(callback, "🔄 Пробую восстановить сессию...", show_alert=False)
        await asyncio.sleep(0.3)

        if hasattr(client, '_session_recovered') and client._session_recovered:
            await callback_feedback(callback, "✅ Сессия успешно восстановлена!", show_alert=False)
            client._session_recovered = False
        elif hasattr(client, '_session_recovery_failed') and client._session_recovery_failed:
            await callback_feedback(callback, "❌ Не удалось восстановить сессию", show_alert=False)
            client._session_recovery_failed = False

        client._recovery_attempted = False

@dp.callback_query(F.data == "back_to_start")
async def process_back_to_start(callback: CallbackQuery, state: FSMContext):
    """Возврат в главное меню"""
    chat = getattr(callback.message, "chat", None)
    if getattr(chat, "type", ChatType.PRIVATE) != ChatType.PRIVATE:
        await callback_feedback(
            callback,
            "🔒 Главное меню доступно только в личном чате с ботом.",
            show_alert=True,
        )
        return
    await callback_feedback(callback)

    # Очищаем состояние FSM
    current_state = await state.get_state()
    if current_state:
        await state.clear()

    # Очищаем пользовательские состояния
    user_id = callback.from_user.id
    if user_id in user_states:
        await clear_legacy_user_state(user_id)
        logger.info(f"Cleared state for user {user_id}")

    user = callback.from_user

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.telegram_id == user.id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

    view = build_main_menu_view(
        user.full_name or user.username or "пользователь",
        mafiles,
        telegram_id=user.id,
    )
    await edit_main_menu(callback.message, view)
