"""Extracted from the legacy bot module without behavior changes."""

from contextlib import suppress
from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from datetime import UTC, datetime
from guardbot.bot.callback_validation import validated_callback_data
from guardbot.bot.handlers.accounts import callback_account_detail, callback_account_settings
from guardbot.bot.handlers.common import cmd_start_help
from guardbot.bot.handlers.confirmations import (
    callback_confirm_all,
    callback_confirmations_menu,
)
from guardbot.bot.handlers.inventory import callback_inventory_menu
from guardbot.bot.handlers.trade_notifications import callback_trades_menu
from guardbot.bot.runtime import (
    callback_feedback,
    check_proxy_available,
    clear_user_workflows,
    delete_secret_message_or_abort,
    start_trade_check_task,
    stop_trade_check_task,
)
from guardbot.bot.states import BotPasswordFlow, TradeNotificationFlow, UnlockFlow
from guardbot.config import logger
from guardbot.database import AsyncSessionLocal, Mafile, User
from guardbot.passwords import (
    MIN_BOT_PASSWORD_LENGTH,
    PasswordRateLimitError,
    hash_bot_password_async,
    password_hash_needs_upgrade,
    verify_bot_password_async,
)
from guardbot.security import decrypt_dict, decrypt_value, encrypt_value
from guardbot.services.ownership import get_owned_mafile
from guardbot.services.session_manager import SteamSessionManager
from guardbot.steam.client import InvalidCredentials, LoginConfirmType, generate_device_id, redact_proxy_url
from sqlalchemy import select
from typing import Dict, Optional
import asyncio
import io
import json
import time
import zipfile

router = Router(name=__name__)

# ==================== КОМАНДА: ИНВЕНТАРЬ ====================
@router.message(Command("inventory"))
@router.message(Command("inv"))
async def cmd_inventory(
    message: Message,
    command: CommandObject,
    state: FSMContext,
):
    """Открыть меню инвентаря аккаунта"""
    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id
    await clear_user_workflows(telegram_id, state)

    if message.chat.type != ChatType.PRIVATE:
        await message.answer(
            "❌ Инвентарь доступен только в личном чате с ботом."
        )
        return

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.telegram_id == telegram_id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await message.answer("❌ У вас нет сохраненных аккаунтов")
            return

        mafile = None

        if not args:
            keyboard_buttons = []
            for idx, mf in enumerate(mafiles, 1):
                keyboard_buttons.append([InlineKeyboardButton(
                    text=f"{idx}. {mf.account_name}",
                    callback_data=f"inventory_menu_{mf.id}"
                )])

            keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

            await message.answer(
                "🎒 <b>Выберите аккаунт для просмотра инвентаря:</b>",
                parse_mode="HTML",
                reply_markup=keyboard
            )
            return

        account_input = args[0]
        if account_input.isdigit():
            idx = int(account_input) - 1
            if 0 <= idx < len(mafiles):
                mafile = mafiles[idx]
        else:
            mafile = next((mf for mf in mafiles if mf.account_name.lower() == account_input.lower()), None)

        if not mafile:
            await message.answer(
                f"❌ Аккаунт <b>{escape(account_input)}</b> не найден",
                parse_mode="HTML",
            )
            return

        if not mafile.steamid:
            await message.answer("❌ SteamID не привязан к аккаунту")
            return

        msg = await message.answer("...", parse_mode="HTML")

        class FakeCallback:
            def __init__(self, message, from_user, data):
                self.message = message
                self.from_user = from_user
                self.data = data
            async def answer(self, text=None, show_alert=False):
                pass

        fake_callback = FakeCallback(
            message=msg,
            from_user=message.from_user,
            data=f"inventory_menu_{mafile.id}"
        )

        await callback_inventory_menu(fake_callback, state)
# ==================== КОМАНДА: ПОДТВЕРЖДЕНИЯ ====================
@router.message(Command("confirmations"))
@router.message(Command("conf"))
async def cmd_confirmations(message: Message, command: CommandObject):
    """Открыть меню подтверждений аккаунта"""
    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(
            Mafile.telegram_id == telegram_id,
            Mafile.identity_secret.isnot(None)
        )
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await message.answer(
                "❌ У вас нет аккаунтов с настроенными подтверждениями\n"
                "Добавьте аккаунт: /add"
            )
            return

        mafile = None

        if not args:
            keyboard_buttons = []
            for idx, mf in enumerate(mafiles, 1):
                keyboard_buttons.append([InlineKeyboardButton(
                    text=f"{idx}. {mf.account_name}",
                    callback_data=f"confirmations_menu_{mf.id}"
                )])

            keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

            await message.answer(
                "✅ <b>Выберите аккаунт для просмотра подтверждений:</b>",
                parse_mode="HTML",
                reply_markup=keyboard
            )
            return

        account_input = args[0]
        if account_input.isdigit():
            idx = int(account_input) - 1
            if 0 <= idx < len(mafiles):
                mafile = mafiles[idx]
        else:
            mafile = next((mf for mf in mafiles if mf.account_name.lower() == account_input.lower()), None)

        if not mafile:
            await message.answer(
                f"❌ Аккаунт <b>{escape(account_input)}</b> не найден",
                parse_mode="HTML",
            )
            return

        if not mafile.access_token:
            await message.answer(
                f"❌ <b>Нет активной сессии!</b>\n\n"
                f"Сначала войдите в аккаунт:\n"
                f"<code>/login {escape(str(mafile.account_name))}</code>",
                parse_mode="HTML"
            )
            return

        msg = await message.answer("...", parse_mode="HTML")

        class FakeCallback:
            def __init__(self, message, from_user, data):
                self.message = message
                self.from_user = from_user
                self.data = data
            async def answer(self, text=None, show_alert=False):
                pass

        fake_callback = FakeCallback(
            message=msg,
            from_user=message.from_user,
            data=f"confirmations_menu_{mafile.id}"
        )

        await callback_confirmations_menu(fake_callback)

# ==================== КОМАНДА: ПОДТВЕРДИТЬ ВСЁ ====================
@router.message(Command("confirmall"))
@router.message(Command("conf_all"))
async def cmd_confirmall(message: Message, command: CommandObject):
    """Подтвердить все ожидающие действия для аккаунта"""
    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(
            Mafile.telegram_id == telegram_id,
            Mafile.identity_secret.isnot(None)
        )
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await message.answer("❌ У вас нет аккаунтов с настроенными подтверждениями")
            return

        mafile = None

        if not args:
            keyboard_buttons = []
            for idx, mf in enumerate(mafiles, 1):
                keyboard_buttons.append([InlineKeyboardButton(
                    text=f"{idx}. {mf.account_name}",
                    callback_data=f"confirm_all_{mf.id}"
                )])

            keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

            await message.answer(
                "✅ <b>Выберите аккаунт для подтверждения всех действий:</b>",
                parse_mode="HTML",
                reply_markup=keyboard
            )
            return

        account_input = args[0]
        if account_input.isdigit():
            idx = int(account_input) - 1
            if 0 <= idx < len(mafiles):
                mafile = mafiles[idx]
        else:
            mafile = next((mf for mf in mafiles if mf.account_name.lower() == account_input.lower()), None)

        if not mafile:
            await message.answer(
                f"❌ Аккаунт <b>{escape(account_input)}</b> не найден",
                parse_mode="HTML",
            )
            return

        if not mafile.access_token:
            await message.answer(
                f"❌ <b>Нет активной сессии!</b>\n\n"
                f"Сначала войдите в аккаунт:\n"
                f"<code>/login {escape(str(mafile.account_name))}</code>",
                parse_mode="HTML"
            )
            return

        msg = await message.answer("...", parse_mode="HTML")

        class FakeCallback:
            def __init__(self, message, from_user, data):
                self.message = message
                self.from_user = from_user
                self.data = data
            async def answer(self, text=None, show_alert=False):
                pass

        fake_callback = FakeCallback(
            message=msg,
            from_user=message.from_user,
            data=f"confirm_all_{mafile.id}"
        )

        await callback_confirm_all(fake_callback)

# ==================== КОМАНДА: ТРЕЙДЫ ====================
@router.message(Command("trades"))
@router.message(Command("trade"))
async def cmd_trades(message: Message, command: CommandObject):
    """Открыть меню трейдов аккаунта"""
    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.telegram_id == telegram_id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await message.answer("❌ У вас нет сохраненных аккаунтов")
            return

        mafile = None

        if not args:
            keyboard_buttons = []
            for idx, mf in enumerate(mafiles, 1):
                keyboard_buttons.append([InlineKeyboardButton(
                    text=f"{idx}. {mf.account_name}",
                    callback_data=f"trades_menu_{mf.id}"
                )])

            keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

            await message.answer(
                "🔄 <b>Выберите аккаунт для просмотра трейдов:</b>",
                parse_mode="HTML",
                reply_markup=keyboard
            )
            return

        account_input = args[0]
        if account_input.isdigit():
            idx = int(account_input) - 1
            if 0 <= idx < len(mafiles):
                mafile = mafiles[idx]
        else:
            mafile = next((mf for mf in mafiles if mf.account_name.lower() == account_input.lower()), None)

        if not mafile:
            await message.answer(
                f"❌ Аккаунт <b>{escape(account_input)}</b> не найден",
                parse_mode="HTML",
            )
            return

        if not mafile.access_token:
            await message.answer(
                f"❌ <b>Нет активной сессии!</b>\n\n"
                f"Сначала войдите в аккаунт:\n"
                f"<code>/login {escape(str(mafile.account_name))}</code>",
                parse_mode="HTML"
            )
            return

        has_proxy, _ = await check_proxy_available(telegram_id, for_steam_requests=True)
        if not has_proxy:
            await message.answer(
                "❌ <b>Нет доступного прокси!</b>\n\n"
                "Для просмотра трейдов необходим прокси.",
                parse_mode="HTML"
            )
            return

        msg = await message.answer("...", parse_mode="HTML")

        class FakeCallback:
            def __init__(self, message, from_user, data):
                self.message = message
                self.from_user = from_user
                self.data = data
            async def answer(self, text=None, show_alert=False):
                pass

        fake_callback = FakeCallback(
            message=msg,
            from_user=message.from_user,
            data=f"trades_menu_{mafile.id}"
        )

        await callback_trades_menu(fake_callback)

# ==================== КОМАНДА: НАСТРОЙКИ АККАУНТА ====================
@router.message(Command("settings"))
@router.message(Command("acc_settings"))
async def cmd_account_settings_cmd(message: Message, command: CommandObject):
    """Открыть настройки конкретного аккаунта"""
    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.telegram_id == telegram_id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await message.answer("❌ У вас нет сохраненных аккаунтов")
            return

        mafile = None

        if not args:
            keyboard_buttons = []
            for idx, mf in enumerate(mafiles, 1):
                keyboard_buttons.append([InlineKeyboardButton(
                    text=f"{idx}. {mf.account_name}",
                    callback_data=f"account_settings_{mf.id}"
                )])

            keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

            await message.answer(
                "⚙️ <b>Выберите аккаунт для настройки:</b>",
                parse_mode="HTML",
                reply_markup=keyboard
            )
            return

        account_input = args[0]
        if account_input.isdigit():
            idx = int(account_input) - 1
            if 0 <= idx < len(mafiles):
                mafile = mafiles[idx]
        else:
            mafile = next((mf for mf in mafiles if mf.account_name.lower() == account_input.lower()), None)

        if not mafile:
            await message.answer(
                f"❌ Аккаунт <b>{escape(account_input)}</b> не найден",
                parse_mode="HTML",
            )
            return

        msg = await message.answer("...", parse_mode="HTML")

        class FakeCallback:
            def __init__(self, message, from_user, data):
                self.message = message
                self.from_user = from_user
                self.data = data
            async def answer(self, text=None, show_alert=False):
                pass

        fake_callback = FakeCallback(
            message=msg,
            from_user=message.from_user,
            data=f"account_settings_{mafile.id}"
        )

        await callback_account_settings(fake_callback)

# ==================== КОМАНДА: ДЕТАЛИ АККАУНТА ====================
@router.message(Command("acc"))
@router.message(Command("account"))
async def cmd_account_detail(message: Message, command: CommandObject):
    """Открыть детали аккаунта"""
    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.telegram_id == telegram_id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await message.answer("❌ У вас нет сохраненных аккаунтов")
            return

        mafile = None

        if not args:
            keyboard_buttons = []
            for idx, mf in enumerate(mafiles, 1):
                keyboard_buttons.append([InlineKeyboardButton(
                    text=f"{idx}. {mf.account_name}",
                    callback_data=f"account_detail_{mf.id}"
                )])

            keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

            await message.answer(
                "👤 <b>Выберите аккаунт для просмотра:</b>",
                parse_mode="HTML",
                reply_markup=keyboard
            )
            return

        account_input = args[0]
        if account_input.isdigit():
            idx = int(account_input) - 1
            if 0 <= idx < len(mafiles):
                mafile = mafiles[idx]
        else:
            mafile = next((mf for mf in mafiles if mf.account_name.lower() == account_input.lower()), None)

        if not mafile:
            await message.answer(
                f"❌ Аккаунт <b>{escape(account_input)}</b> не найден",
                parse_mode="HTML",
            )
            return

        msg = await message.answer("...", parse_mode="HTML")

        class FakeCallback:
            def __init__(self, message, from_user, data):
                self.message = message
                self.from_user = from_user
                self.data = data
            async def answer(self, text=None, show_alert=False):
                pass

        fake_callback = FakeCallback(
            message=msg,
            from_user=message.from_user,
            data=f"account_detail_{mafile.id}"
        )

        await callback_account_detail(fake_callback)

# ==================== КОМАНДА: ЗАМЕТКА ====================
@router.message(Command("note"))
async def cmd_note(message: Message, command: CommandObject):
    """Добавить/посмотреть/удалить заметку к аккаунту"""
    if not await delete_secret_message_or_abort(message):
        return
    if message.chat.type != ChatType.PRIVATE:
        await message.answer(
            "❌ Заметки можно использовать только в личном чате с ботом."
        )
        return

    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.telegram_id == telegram_id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await message.answer("❌ У вас нет сохраненных аккаунтов")
            return

        if not args:
            await message.answer(
                "📝 <b>Использование:</b>\n"
                "<code>/note [acc]</code> — посмотреть заметку\n"
                "<code>/note [acc] [текст]</code> — сохранить заметку\n"
                "<code>/note [acc] .</code> — удалить заметку",
                parse_mode="HTML"
            )
            return

        account_input = args[0]
        mafile = None

        if account_input.isdigit():
            idx = int(account_input) - 1
            if 0 <= idx < len(mafiles):
                mafile = mafiles[idx]
        else:
            mafile = next((mf for mf in mafiles if mf.account_name.lower() == account_input.lower()), None)

        if not mafile:
            await message.answer(
                f"❌ Аккаунт <b>{escape(account_input)}</b> не найден",
                parse_mode="HTML",
            )
            return

        if len(args) == 1:

            # Показать заметку
            current_note = decrypt_value(mafile.notes) if mafile.notes else None
            if current_note:
                await message.answer(
                    f"📝 <b>Заметка для {escape(str(mafile.account_name))}:</b>\n"
                    f"<i>{escape(str(current_note))}</i>\n\n"
                    f"<code>/note {escape(str(account_input))} .</code> — удалить",
                    parse_mode="HTML"
                )
            else:
                await message.answer(
                    f"📝 <b>{escape(str(mafile.account_name))}</b> — заметок нет\n"
                    f"<code>/note {escape(str(account_input))} текст</code> — добавить",
                    parse_mode="HTML"
                )
            return

        # Сохраняем или удаляем заметку
        note_text = ' '.join(args[1:])

        if note_text == '.':
            mafile.notes = None
            await session.commit()
            await message.answer(f"✅ Заметка для <b>{escape(str(mafile.account_name))}</b> удалена", parse_mode="HTML")
        else:
            if len(note_text) > 500:
                note_text = note_text[:500]
            mafile.notes = encrypt_value(note_text)
            await session.commit()
            await message.answer(
                f"✅ Заметка для <b>{escape(str(mafile.account_name))}</b> сохранена:\n"
                f"<i>{escape(str(note_text))}</i>",
                parse_mode="HTML"
            )


# ==================== КОМАНДА: ПРОКСИ АККАУНТА ====================
@router.message(Command("proxy"))
async def cmd_proxy_info(message: Message, command: CommandObject):
    """Показать какой прокси используется для аккаунта"""
    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.telegram_id == telegram_id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await message.answer("❌ У вас нет сохраненных аккаунтов")
            return

        if not args:
            await message.answer(
                "❌ <b>Использование:</b> <code>/proxy [acc]</code>\n\n"
                "Показывает какой прокси используется для аккаунта.",
                parse_mode="HTML"
            )
            return

        account_input = args[0]
        mafile = None

        if account_input.isdigit():
            idx = int(account_input) - 1
            if 0 <= idx < len(mafiles):
                mafile = mafiles[idx]
        else:
            mafile = next((mf for mf in mafiles if mf.account_name.lower() == account_input.lower()), None)

        if not mafile:
            await message.answer(
                f"❌ Аккаунт <b>{escape(account_input)}</b> не найден",
                parse_mode="HTML",
            )
            return

        if mafile.unique_proxy and mafile.proxy:
            decrypted = decrypt_value(mafile.proxy)
            await message.answer(
                f"🌐 <b>Прокси для {escape(str(mafile.account_name))}:</b>\n"
                f"🔒 Уникальный:\n<code>{escape(str(redact_proxy_url(decrypted)))}</code>",
                parse_mode="HTML"
            )
        else:
            stmt = select(User).where(User.telegram_id == telegram_id)
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()

            if user and user.general_proxy:
                decrypted = decrypt_value(user.general_proxy)
                await message.answer(
                    f"🌐 <b>Прокси для {escape(str(mafile.account_name))}:</b>\n"
                    f"🌍 Общий:\n<code>{escape(str(redact_proxy_url(decrypted)))}</code>",
                    parse_mode="HTML"
                )
            else:
                await message.answer(
                    f"❌ <b>Прокси для {escape(str(mafile.account_name))} не настроен</b>\n\n"
                    f"<code>/set_proxy [url]</code> — общий прокси\n"
                    f"<code>/set_account_proxy {escape(str(mafile.account_name))} [url]</code> — уникальный",
                    parse_mode="HTML"
                )


# ==================== КОМАНДА: STEAMID ====================
@router.message(Command("steamid"))
async def cmd_steamid(message: Message, command: CommandObject):
    """Показать SteamID аккаунта"""
    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.telegram_id == telegram_id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await message.answer("❌ У вас нет сохраненных аккаунтов")
            return

        if not args:
            await message.answer(
                "❌ <b>Использование:</b> <code>/steamid [acc]</code>",
                parse_mode="HTML"
            )
            return

        account_input = args[0]
        mafile = None

        if account_input.isdigit():
            idx = int(account_input) - 1
            if 0 <= idx < len(mafiles):
                mafile = mafiles[idx]
        else:
            mafile = next((mf for mf in mafiles if mf.account_name.lower() == account_input.lower()), None)

        if not mafile:
            await message.answer(
                f"❌ Аккаунт <b>{escape(account_input)}</b> не найден",
                parse_mode="HTML",
            )
            return

        if not mafile.steamid:
            await message.answer(
                f"❌ <b>{escape(str(mafile.account_name))}</b> — SteamID не привязан\n"
                f"Сначала войдите в аккаунт: <code>/login {escape(str(mafile.account_name))}</code>",
                parse_mode="HTML"
            )
            return

        steamid64 = str(mafile.steamid)
        profile_url = f"https://steamcommunity.com/profiles/{steamid64}"

        # Конвертируем SteamID64 в SteamID (для красоты)
        try:
            sid64 = int(steamid64)
            universe = (sid64 >> 56) & 0xFF
            account_id = sid64 & 0xFFFFFFFF
            steamid2 = f"STEAM_0:{account_id & 1}:{account_id >> 1}"
            steamid3 = f"[U:1:{account_id}]"
        except:
            steamid2 = "N/A"
            steamid3 = "N/A"

        await message.answer(
            f"🆔 <b>{escape(str(mafile.account_name))}</b>\n\n"
            f"├─ SteamID: <code>{steamid2}</code>\n"
            f"├─ SteamID3: <code>{steamid3}</code>\n"
            f"├─ SteamID64: <code>{steamid64}</code>\n"
            f"└─ <a href='{escape(str(profile_url))}'>Профиль Steam</a>",
            parse_mode="HTML",
            disable_web_page_preview=True
        )


# ==================== КОМАНДА: ТРЕЙД-ССЫЛКА ====================
@router.message(Command("tradelink"))
@router.message(Command("tl"))
async def cmd_tradelink(message: Message, command: CommandObject):
    """Получить трейд-ссылку аккаунта"""
    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.telegram_id == telegram_id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await message.answer("❌ У вас нет сохраненных аккаунтов")
            return

        if not args:
            await message.answer(
                "❌ <b>Использование:</b> <code>/tradelink [acc]</code>",
                parse_mode="HTML"
            )
            return

        account_input = args[0]
        mafile = None

        if account_input.isdigit():
            idx = int(account_input) - 1
            if 0 <= idx < len(mafiles):
                mafile = mafiles[idx]
        else:
            mafile = next((mf for mf in mafiles if mf.account_name.lower() == account_input.lower()), None)

        if not mafile:
            await message.answer(
                f"❌ Аккаунт <b>{escape(account_input)}</b> не найден",
                parse_mode="HTML",
            )
            return

        if not mafile.access_token:
            await message.answer(
                f"❌ <b>Нет активной сессии!</b>\n\n"
                f"Сначала войдите в аккаунт:\n"
                f"<code>/login {escape(str(mafile.account_name))}</code>",
                parse_mode="HTML"
            )
            return

        status_msg = await message.answer("🔄 <b>Получаю трейд-ссылку...</b>", parse_mode="HTML")

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            tradelink = await client.get_tradelink()

            if tradelink:
                await status_msg.edit_text(
                    f"🔗 <b>Трейд-ссылка для {escape(str(mafile.account_name))}:</b>\n\n"
                    f"<code>{escape(str(tradelink))}</code>",
                    parse_mode="HTML"
                )
            else:
                await status_msg.edit_text(
                    f"❌ <b>Не удалось получить трейд-ссылку</b>\n\n"
                    f"Возможные причины:\n"
                    f"• Сессия устарела\n"
                    f"• Аккаунт ограничен\n"
                    f"• Не настроен обмен\n\n"
                    f"Попробуйте войти заново: <code>/login {escape(str(mafile.account_name))}</code>",
                    parse_mode="HTML"
                )
        except Exception as e:
            await status_msg.edit_text(
                f"❌ Ошибка: {escape(str(e)[:200])}",
                parse_mode="HTML",
            )
        finally:
            await client.close()


# ==================== КОМАНДА: МАССОВЫЙ ВХОД ====================
@router.message(Command("login_all"))
async def cmd_login_all(message: Message):
    """Войти во все аккаунты (сначала через refresh token, потом полный вход)"""
    telegram_id = message.from_user.id

    # Проверяем прокси
    has_proxy, _ = await check_proxy_available(telegram_id, for_steam_requests=True)
    if not has_proxy:
        await message.answer(
            "❌ <b>Нет доступного прокси!</b>\n\n"
            "Для входа в аккаунты необходим прокси.",
            parse_mode="HTML"
        )
        return

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.telegram_id == telegram_id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await message.answer("❌ У вас нет сохраненных аккаунтов")
            return

        status_msg = await message.answer(
            f"🔄 <b>Начинаю вход в {len(mafiles)} аккаунтов...</b>\n"
            f"Задержка между аккаунтами: 5 сек",
            parse_mode="HTML"
        )

        success = []
        need_code = []
        errors = []

        for idx, mafile in enumerate(mafiles, 1):
            # Обновляем статус
            await status_msg.edit_text(
                f"🔄 <b>Вход в аккаунты ({idx}/{len(mafiles)})</b>\n\n"
                f"✅ Успешно: {len(success)}\n"
                f"⚠️ Требуют код: {len(need_code)}\n"
                f"❌ Ошибки: {len(errors)}\n\n"
                f"<i>Текущий: {escape(str(mafile.account_name))}...</i>",
                parse_mode="HTML"
            )

            if not mafile.password:
                errors.append(f"{mafile.account_name}: нет пароля")
                continue

            try:
                client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

                # Пробуем через refresh token
                if mafile.refresh_token and not client.is_token_expired(decrypt_value(mafile.refresh_token)):
                    try:
                        client.steamid = str(mafile.steamid) if mafile.steamid else None
                        client.refresh_token = decrypt_value(mafile.refresh_token)
                        client.access_token = decrypt_value(mafile.access_token)
                        await client.refresh_access_token()
                        await client._ensure_sessionid()
                        await SteamSessionManager.save_steam_session_to_db(session, mafile.id, client)
                        success.append(mafile.account_name)
                        await client.close()

                        # Задержка между аккаунтами
                        if idx < len(mafiles):
                            await asyncio.sleep(5)
                        continue
                    except:
                        pass

                # Пробуем полный вход
                try:
                    code_type = await client.login()

                    if code_type == LoginConfirmType.none:
                        await client.confirm_login()
                        await SteamSessionManager.save_steam_session_to_db(session, mafile.id, client)
                        success.append(mafile.account_name)
                    elif code_type == LoginConfirmType.email:
                        await client.confirm_login()
                        await SteamSessionManager.save_steam_session_to_db(session, mafile.id, client)
                        success.append(mafile.account_name)
                    else:
                        if mafile.shared_secret:
                            client.load_mobile({
                                'shared_secret': decrypt_value(mafile.shared_secret),
                                'device_id': decrypt_value(mafile.device_id) or generate_device_id()
                            })
                            await client.align_time()
                            code = client.generate_steam_guard_code()
                            await client.confirm_login(code)
                            await SteamSessionManager.save_steam_session_to_db(session, mafile.id, client)
                            success.append(mafile.account_name)
                        else:
                            need_code.append(mafile.account_name)
                except InvalidCredentials:
                    errors.append(f"{mafile.account_name}: неверный логин/пароль")
                except Exception as e:
                    errors.append(f"{mafile.account_name}: {str(e)[:50]}")
                finally:
                    await client.close()

            except Exception as e:
                errors.append(f"{mafile.account_name}: {str(e)[:50]}")

            # Задержка между аккаунтами
            if idx < len(mafiles):
                await asyncio.sleep(5)

        # Финальный отчёт
        result_text = f"📊 <b>РЕЗУЛЬТАТЫ ВХОДА ({len(mafiles)} акк.)</b>\n\n"

        if success:
            result_text += f"✅ <b>Успешно ({len(success)}):</b>\n"
            for name in success:
                result_text += f"  • {escape(str(name))}\n"
            result_text += "\n"

        if need_code:
            result_text += f"⚠️ <b>Требуют код ({len(need_code)}):</b>\n"
            for name in need_code:
                escaped_name = escape(str(name))
                result_text += f"  • {escaped_name} — <code>/login {escaped_name}</code>\n"
            result_text += "\n"

        if errors:
            result_text += f"❌ <b>Ошибки ({len(errors)}):</b>\n"
            for err in errors:
                result_text += f"  • {escape(str(err))}\n"

        await status_msg.edit_text(result_text, parse_mode="HTML")


# ==================== КОМАНДА: ВКЛЮЧИТЬ УВЕДОМЛЕНИЯ ====================
@router.message(Command("notify_on"))
async def cmd_notify_on(
    message: Message,
    command: CommandObject,
    state: FSMContext,
):
    """Включить уведомления о трейдах"""
    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id
    await clear_user_workflows(telegram_id, state)

    if message.chat.type != ChatType.PRIVATE:
        await message.answer(
            "❌ Настраивать уведомления можно только в личном чате с ботом."
        )
        return

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.telegram_id == telegram_id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await message.answer("❌ У вас нет сохраненных аккаунтов")
            return

        if not args:
            await message.answer(
                "❌ <b>Использование:</b> <code>/notify_on [acc] [минуты]</code>\n\n"
                "Пример: <code>/notify_on mylogin 5</code>",
                parse_mode="HTML"
            )
            return

        account_input = args[0]
        mafile = None

        if account_input.isdigit():
            idx = int(account_input) - 1
            if 0 <= idx < len(mafiles):
                mafile = mafiles[idx]
        else:
            mafile = next((mf for mf in mafiles if mf.account_name.lower() == account_input.lower()), None)

        if not mafile:
            await message.answer(
                f"❌ Аккаунт <b>{escape(account_input)}</b> не найден",
                parse_mode="HTML",
            )
            return

        if len(args) >= 2:
            try:
                interval = int(args[1])
                if not 1 <= interval <= 60:
                    await message.answer("❌ Интервал должен быть от 1 до 60 минут")
                    return

                mafile.trade_notifications = True
                mafile.trade_notify_interval = interval
                mafile.known_trade_ids = []
                await session.commit()

                start_trade_check_task(mafile.id)

                await message.answer(
                    f"✅ <b>Уведомления включены!</b>\n\n"
                    f"📱 Аккаунт: <code>{escape(mafile.account_name)}</code>\n"
                    f"⏱ Интервал: каждые {interval} мин",
                    parse_mode="HTML"
                )
            except ValueError:
                await message.answer("❌ Интервал должен быть числом")
        else:
            try:
                await state.set_state(TradeNotificationFlow.waiting_interval)
                await state.set_data({"mafile_id": mafile.id, "is_update": False})
                await message.answer(
                    "⏱ <b>Введите интервал проверки в минутах:</b>\n\n"
                    f"Аккаунт: <code>{escape(mafile.account_name)}</code>\n\n"
                    "Отправьте число (1-60):",
                    parse_mode="HTML",
                )
            except (Exception, asyncio.CancelledError):
                await clear_user_workflows(telegram_id, state)
                raise


# ==================== КОМАНДА: ВЫКЛЮЧИТЬ УВЕДОМЛЕНИЯ ====================
@router.message(Command("notify_off"))
async def cmd_notify_off(message: Message, command: CommandObject):
    """Выключить уведомления о трейдах"""
    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.telegram_id == telegram_id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await message.answer("❌ У вас нет сохраненных аккаунтов")
            return

        if not args:
            await message.answer(
                "❌ <b>Использование:</b> <code>/notify_off [acc]</code>",
                parse_mode="HTML"
            )
            return

        account_input = args[0]
        mafile = None

        if account_input.isdigit():
            idx = int(account_input) - 1
            if 0 <= idx < len(mafiles):
                mafile = mafiles[idx]
        else:
            mafile = next((mf for mf in mafiles if mf.account_name.lower() == account_input.lower()), None)

        if not mafile:
            await message.answer(
                f"❌ Аккаунт <b>{escape(account_input)}</b> не найден",
                parse_mode="HTML",
            )
            return

        mafile.trade_notifications = False
        await session.commit()

        stop_trade_check_task(mafile.id)

        await message.answer(
            f"✅ <b>Уведомления выключены!</b>\n\n"
            f"📱 Аккаунт: <code>{escape(str(mafile.account_name))}</code>",
            parse_mode="HTML"
        )

# ==================== КОМАНДА: ЭКСПОРТ MAFILE ====================
@router.message(Command("export"))
async def cmd_export_mafile(message: Message, command: CommandObject):
    """Экспорт mafile для одного аккаунта"""
    if message.chat.type != ChatType.PRIVATE:
        await message.answer(
            "❌ Экспорт доступен только в личном чате с ботом."
        )
        return

    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.telegram_id == telegram_id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await message.answer("❌ У вас нет сохраненных аккаунтов")
            return

        if not args:
            keyboard_buttons = []
            for idx, mf in enumerate(mafiles, 1):
                keyboard_buttons.append([InlineKeyboardButton(
                    text=f"{idx}. {mf.account_name}",
                    callback_data=f"export_mafile_{mf.id}"
                )])

            keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

            await message.answer(
                "📤 <b>Выберите аккаунт для экспорта:</b>",
                parse_mode="HTML",
                reply_markup=keyboard
            )
            return

        account_input = args[0]
        mafile = None

        if account_input.isdigit():
            idx = int(account_input) - 1
            if 0 <= idx < len(mafiles):
                mafile = mafiles[idx]
        else:
            mafile = next((mf for mf in mafiles if mf.account_name.lower() == account_input.lower()), None)

        if not mafile:
            await message.answer(
                f"❌ Аккаунт <b>{escape(account_input)}</b> не найден",
                parse_mode="HTML",
            )
            return

        await send_mafile(message, mafile)


# ==================== КОМАНДА: ЭКСПОРТ ВСЕХ MAFILE ====================
@router.message(Command("export_all"))
async def cmd_export_all(message: Message):
    """Экспорт всех аккаунтов в ZIP архив"""
    if message.chat.type != ChatType.PRIVATE:
        await message.answer(
            "❌ Экспорт доступен только в личном чате с ботом."
        )
        return

    telegram_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.telegram_id == telegram_id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await message.answer("❌ У вас нет сохраненных аккаунтов")
            return

        if len(mafiles) == 1:
            # Если один аккаунт — отправляем просто файл
            await send_mafile(message, mafiles[0])
            return

        status_msg = await message.answer(
            f"🔄 <b>Создаю архив из {len(mafiles)} аккаунтов...</b>",
            parse_mode="HTML"
        )

        try:
            # Создаём ZIP архив в памяти
            zip_buffer = io.BytesIO()

            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for mafile in mafiles:
                    mafile_data = build_mafile_dict(mafile)

                    if mafile_data:
                        json_str = json.dumps(mafile_data, indent=2, ensure_ascii=False)
                        filename = f"{mafile.account_name}.mafile"
                        zip_file.writestr(filename, json_str)

            zip_buffer.seek(0)


            await status_msg.edit_text(
                f"📤 <b>Экспорт {len(mafiles)} аккаунтов</b>\n\n"
                f"Файлы в архиве:\n" +
                "\n".join([f"• {escape(str(mf.account_name))}.mafile" for mf in mafiles]),
                parse_mode="HTML"
            )

            await message.answer_document(
                BufferedInputFile(
                    zip_buffer.getvalue(),
                    filename="steam_accounts.zip"
                ),
                caption=f"📦 {len(mafiles)} аккаунтов Steam"
            )

        except Exception as e:
            await status_msg.edit_text(
                f"❌ Ошибка при создании архива: {escape(str(e)[:200])}",
                parse_mode="HTML"
            )


# ==================== CALLBACK: ЭКСПОРТ MAFILE ====================
@router.callback_query(F.data.startswith("export_mafile_"))
@validated_callback_data(r"export_mafile_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_export_mafile(callback: CallbackQuery):
    """Экспорт mafile через кнопку"""
    mafile_id = int(callback.data.split("_")[2])

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        await send_mafile(callback.message, mafile)
        await callback_feedback(callback, "✅ Файл отправлен!")


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def build_mafile_dict(mafile: Mafile) -> Optional[Dict]:
    """Создаёт словарь с данными mafile для экспорта (формат SDA)"""
    try:
        # Получаем расшифрованные данные
        shared_secret = decrypt_value(mafile.shared_secret) if mafile.shared_secret else None
        identity_secret = decrypt_value(mafile.identity_secret) if mafile.identity_secret else None
        device_id = decrypt_value(mafile.device_id) if mafile.device_id else None
        serial_number = decrypt_value(mafile.serial_number) if mafile.serial_number else None
        revocation_code = decrypt_value(mafile.revocation_code) if mafile.revocation_code else None
        token_gid = decrypt_value(mafile.token_gid) if mafile.token_gid else None
        uri = decrypt_value(mafile.uri) if mafile.uri else None
        secret_1 = decrypt_value(mafile.secret_1) if mafile.secret_1 else None
        access_token = decrypt_value(mafile.access_token) if mafile.access_token else None
        refresh_token = decrypt_value(mafile.refresh_token) if mafile.refresh_token else None

        # SteamID64
        steamid64 = mafile.steamid

        # Получаем session_data если есть
        session_id = None
        if mafile.session_data_encrypted:
            session_data = decrypt_dict(mafile.session_data_encrypted)
            if session_data:
                session_id = session_data.get('session_id')

        # Формируем словарь в формате SDA
        mafile_data = {}

        # Основные поля
        if shared_secret:
            mafile_data["shared_secret"] = shared_secret
        if serial_number:
            mafile_data["serial_number"] = serial_number
        if revocation_code:
            mafile_data["revocation_code"] = revocation_code
        if uri:
            mafile_data["uri"] = uri

        mafile_data["server_time"] = int(time.time())
        mafile_data["account_name"] = mafile.account_name

        if token_gid:
            mafile_data["token_gid"] = token_gid
        if identity_secret:
            mafile_data["identity_secret"] = identity_secret
        if secret_1:
            mafile_data["secret_1"] = secret_1

        mafile_data["status"] = 1
        mafile_data["device_id"] = device_id or generate_device_id()
        mafile_data["fully_enrolled"] = True

        # Сессия
        if access_token or refresh_token or steamid64:
            session = {}
            if steamid64:
                session["SteamID"] = steamid64
            if access_token:
                session["AccessToken"] = access_token
            if refresh_token:
                session["RefreshToken"] = refresh_token
            if session_id:
                session["SessionID"] = session_id

            mafile_data["Session"] = session

        # Убираем None значения из Session
        if "Session" in mafile_data:
            mafile_data["Session"] = {k: v for k, v in mafile_data["Session"].items() if v is not None}
            if not mafile_data["Session"]:
                del mafile_data["Session"]

        return mafile_data
    except Exception as e:
        logger.error(f"Error building mafile dict for {mafile.account_name}: {e}")
        return None

async def send_mafile(message: Message, mafile: Mafile):
    """Отправляет mafile файл пользователю"""
    if message.chat.type != ChatType.PRIVATE:
        await message.answer(
            "❌ Экспорт доступен только в личном чате с ботом."
        )
        return

    mafile_data = build_mafile_dict(mafile)

    if not mafile_data:
        await message.answer(
            f"❌ <b>Не удалось создать mafile для {escape(str(mafile.account_name))}</b>",
            parse_mode="HTML"
        )
        return

    json_str = json.dumps(mafile_data, indent=2, ensure_ascii=False)


    file_bytes = json_str.encode('utf-8')

    # Формируем описание что внутри файла
    has_2fa = "✅" if mafile_data.get('shared_secret') else "❌"
    has_session = "✅" if 'Session' in mafile_data else "❌"

    caption = (
        f"📤 <b>Экспорт {escape(str(mafile.account_name))}</b>\n"
        f"🔐 2FA: {has_2fa}\n"
        f"🔑 Сессия: {has_session}\n"
        f"🆔 SteamID: {mafile.steamid or 'Нет'}"
    )

    await message.answer_document(
        BufferedInputFile(
            file_bytes,
            filename=f"{mafile.account_name}.mafile"
        ),
        caption=caption,
        parse_mode="HTML"
    )


# ==================== КОМАНДА: УСТАНОВИТЬ ПАРОЛЬ ====================
async def _reject_password_outside_private_chat(message: Message) -> bool:
    """Keep bot-local passwords out of group and channel history."""
    if message.chat.type == ChatType.PRIVATE:
        return False

    with suppress(TelegramAPIError):
        await message.delete()
    await message.answer(
        "❌ Пароли можно вводить только в личном чате с ботом."
    )
    return True


@router.message(Command("set_password"))
async def cmd_set_password(
    message: Message,
    command: CommandObject,
    state: FSMContext,
):
    """Установить пароль для блокировки бота"""
    if await _reject_password_outside_private_chat(message):
        return
    if message.from_user is None:
        return

    telegram_id = message.from_user.id
    password = command.args
    if not password:
        await clear_user_workflows(telegram_id, state)
        try:
            await state.set_state(BotPasswordFlow.waiting_password)
            await state.set_data({"action": "set"})
            await message.answer(
                "🔑 <b>УСТАНОВКА ПАРОЛЯ</b>\n\n"
                "Отправьте пароль для блокировки бота.\n"
                f"<i>Минимум {MIN_BOT_PASSWORD_LENGTH} символов.</i>\n\n"
                "Или используйте:\n"
                "<code>/set_password [пароль]</code>",
                parse_mode="HTML"
            )
        except (Exception, asyncio.CancelledError):
            await clear_user_workflows(telegram_id, state)
            raise
        return

    if not await delete_secret_message_or_abort(message):
        return
    await clear_user_workflows(telegram_id, state)
    await _process_bot_password(message, state, password.strip(), "set")


async def _process_bot_password(
    message: Message,
    state: FSMContext,
    password: str,
    action: str,
) -> None:
    """Apply one validated bot-password action without storing its secret."""
    if message.from_user is None:
        await state.clear()
        return
    telegram_id = message.from_user.id
    if action not in {"set", "change", "remove"}:
        await clear_user_workflows(telegram_id, state)
        await message.answer("❌ Некорректное действие")
        return

    if action != "remove" and len(password) < MIN_BOT_PASSWORD_LENGTH:
        await message.answer(
            f"❌ Пароль должен быть не менее {MIN_BOT_PASSWORD_LENGTH} символов"
        )
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()

        if action == "remove":
            if not user or not user.bot_password:
                await clear_user_workflows(telegram_id, state)
                await message.answer("❌ Пароль не установлен")
                return
            try:
                password_valid = await verify_bot_password_async(
                    user.bot_password,
                    password,
                    user_id=telegram_id,
                )
            except PasswordRateLimitError:
                await clear_user_workflows(telegram_id, state)
                await message.answer(
                    "❌ Слишком много попыток. Повторите через минуту."
                )
                return
            if not password_valid:
                await clear_user_workflows(telegram_id, state)
                await message.answer(
                    "❌ <b>Неверный пароль!</b>",
                    parse_mode="HTML",
                )
                return
            user.bot_password = None
            await session.commit()
        else:
            password_hash = await hash_bot_password_async(password)
            if not user:
                user = User(
                    telegram_id=telegram_id,
                    username=message.from_user.username,
                    full_name=message.from_user.full_name,
                    bot_password=password_hash,
                )
                session.add(user)
            else:
                user.bot_password = password_hash
            user.last_activity = datetime.now(UTC).replace(tzinfo=None)
            await session.commit()

    await clear_user_workflows(telegram_id, state)
    if action == "remove":
        await message.answer("✅ <b>Пароль удалён!</b>", parse_mode="HTML")
        return

    action_text = "изменён" if action == "change" else "установлен"
    await message.answer(
        f"✅ <b>Пароль {action_text}!</b>\n\n"
        "<code>/lock</code> — заблокировать бота\n"
        "<code>/unlock [пароль]</code> — разблокировать",
        parse_mode="HTML",
    )


@router.message(
    BotPasswordFlow.waiting_password,
    F.text,
    ~F.text.startswith("/"),
)
async def receive_bot_password(message: Message, state: FSMContext):
    """Consume a bot-local password only after secure Telegram deletion."""
    if message.from_user is None:
        await state.clear()
        return
    if message.chat.type != ChatType.PRIVATE:
        await delete_secret_message_or_abort(message)
        await message.answer(
            "❌ Пароли можно вводить только в личном чате с ботом."
        )
        return
    if not await delete_secret_message_or_abort(message):
        return

    state_data = await state.get_data()
    action = state_data.get("action")
    if type(action) is not str or action not in {"set", "change", "remove"}:
        await clear_user_workflows(message.from_user.id, state)
        await message.answer("❌ Некорректное состояние настройки пароля")
        return

    await _process_bot_password(
        message,
        state,
        (message.text or "").strip(),
        action,
    )


# ==================== КОМАНДА: ЗАБЛОКИРОВАТЬ БОТА ====================
@router.message(Command("lock"))
async def cmd_lock(message: Message, state: FSMContext):
    """Заблокировать бота"""
    telegram_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if not user or not user.bot_password:
            await message.answer(
                "❌ <b>Пароль не установлен!</b>\n\n"
                "Сначала установите пароль:\n"
                "<code>/set_password [пароль]</code>",
                parse_mode="HTML"
            )
            return

        user.last_activity = None  # Блокируем
        await session.commit()

    await clear_user_workflows(telegram_id, state)
    await message.answer(
        "🔒 <b>БОТ ЗАБЛОКИРОВАН</b>\n\n"
        "Для разблокировки используйте:\n"
        "<code>/unlock [пароль]</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔓 Разблокировать", callback_data="unlock_bot")]
            ]
        )
    )


# ==================== КОМАНДА: РАЗБЛОКИРОВАТЬ БОТА ====================
@router.message(Command("unlock"))
async def cmd_unlock(
    message: Message,
    command: CommandObject,
    state: FSMContext,
):
    """Разблокировать бота"""
    if await _reject_password_outside_private_chat(message):
        return

    password = command.args.strip() if command.args else ""
    telegram_id = message.from_user.id

    # Удаляем сообщение с паролем для безопасности
    try:
        await message.delete()
    except:
        pass

    if not password:
        # Если пароль не передан — запрашиваем
        await clear_user_workflows(telegram_id, state)
        await state.set_state(UnlockFlow.waiting_password)

        try:
            await message.answer(
                "🔓 <b>РАЗБЛОКИРОВКА</b>\n\n"
                "Отправьте пароль для разблокировки бота.\n\n"
                "<i>После отправки бот попытается удалить сообщение с паролем.</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_unlock")]
                    ]
                )
            )
        except Exception:
            await state.clear()
            raise
        return

    async with AsyncSessionLocal() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if not user or not user.bot_password:
            await clear_user_workflows(telegram_id, state)
            await message.answer(
                "❌ <b>Пароль не установлен!</b>\n\n"
                "Сначала установите пароль:\n"
                "<code>/set_password [пароль]</code>",
                parse_mode="HTML"
            )
            return

        try:
            password_valid = await verify_bot_password_async(
                user.bot_password, password, user_id=telegram_id
            )
        except PasswordRateLimitError:
            await message.answer("❌ Слишком много попыток. Повторите через минуту.")
            return

        if not password_valid:
            await message.answer(
                "❌ <b>Неверный пароль!</b>\n\n"
                "Попробуйте еще раз:\n"
                "<code>/unlock [пароль]</code>",
                parse_mode="HTML"
            )
            return

        if password_hash_needs_upgrade(user.bot_password):
            user.bot_password = await hash_bot_password_async(password)

        # Разблокируем — устанавливаем время последней активности
        user.last_activity = datetime.now(UTC).replace(tzinfo=None)
        await session.commit()

    await clear_user_workflows(telegram_id, state)
    # Сообщение об успешной разблокировке
    await message.answer("✅ <b>Бот разблокирован!</b>", parse_mode="HTML")

    # Автоматически показываем главное меню
    await cmd_start_help(message)


@router.callback_query(F.data == "cancel_unlock")
async def callback_cancel_unlock(
    callback: CallbackQuery,
    state: FSMContext,
):
    """Cancel password entry without exposing account data."""
    await callback_feedback(callback)
    await clear_user_workflows(callback.from_user.id, state)
    await callback.message.edit_text(
        "🔒 <b>БОТ ЗАБЛОКИРОВАН</b>\n\n"
        "Для разблокировки используйте:\n"
        "<code>/unlock [пароль]</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔓 Разблокировать", callback_data="unlock_bot")]
            ]
        ),
    )


@router.message(
    UnlockFlow.waiting_password,
    F.text,
    ~F.text.startswith("/"),
)
async def receive_unlock_password(
    message: Message,
    state: FSMContext,
):
    """Verify a bot-local unlock password without storing it in FSM data."""
    if await _reject_password_outside_private_chat(message):
        return

    with suppress(TelegramAPIError):
        await message.delete()

    password = (message.text or "").strip()
    telegram_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()

        if not user or not user.bot_password:
            await state.clear()
            await message.answer("❌ Пароль не установлен")
            return

        try:
            password_valid = await verify_bot_password_async(
                user.bot_password,
                password,
                user_id=telegram_id,
            )
        except PasswordRateLimitError:
            await message.answer(
                "❌ Слишком много попыток. Повторите через минуту."
            )
            return

        if not password_valid:
            await message.answer(
                "❌ <b>Неверный пароль!</b>",
                parse_mode="HTML",
            )
            return

        if password_hash_needs_upgrade(user.bot_password):
            user.bot_password = await hash_bot_password_async(password)

        user.last_activity = datetime.utcnow()
        await session.commit()

    await state.clear()
    await message.answer("✅ <b>Бот разблокирован!</b>", parse_mode="HTML")
    await cmd_start_help(message)


# ==================== КОМАНДА: НАСТРОЙКА АВТОБЛОКИРОВКИ ====================
@router.message(Command("set_lock_timeout"))
async def cmd_set_lock_timeout(message: Message, command: CommandObject):
    """Настроить таймаут автоблокировки"""
    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id

    if not args:
        async with AsyncSessionLocal() as session:
            stmt = select(User).where(User.telegram_id == telegram_id)
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()
            current = user.lock_timeout if user and user.lock_timeout else 0

        await message.answer(
            f"⏱ <b>АВТОБЛОКИРОВКА</b>\n\n"
            f"Текущий таймаут: <b>{current} мин</b>\n\n"
            f"Использование:\n"
            f"<code>/set_lock_timeout [минуты]</code> — установить\n"
            f"<code>/set_lock_timeout 0</code> — выключить\n\n"
            f"Примеры:\n"
            f"<code>/set_lock_timeout 5</code> — блокировка через 5 мин\n"
            f"<code>/set_lock_timeout 30</code> — блокировка через 30 мин",
            parse_mode="HTML"
        )
        return

    try:
        timeout = int(args[0])
        if timeout < 0:
            await message.answer("❌ Таймаут не может быть отрицательным")
            return
    except ValueError:
        await message.answer("❌ Введите число (минуты)")
        return

    async with AsyncSessionLocal() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if not user:
            user = User(
                telegram_id=telegram_id,
                username=message.from_user.username,
                full_name=message.from_user.full_name,
                lock_timeout=timeout if timeout > 0 else None
            )
            session.add(user)
        else:
            user.lock_timeout = timeout if timeout > 0 else None

        await session.commit()

    if timeout > 0:
        await message.answer(
            f"✅ <b>Автоблокировка настроена!</b>\n\n"
            f"Бот будет блокироваться через <b>{timeout} мин</b> бездействия.",
            parse_mode="HTML"
        )
    else:
        await message.answer("✅ <b>Автоблокировка выключена!</b>", parse_mode="HTML")