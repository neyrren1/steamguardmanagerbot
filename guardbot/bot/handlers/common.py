"""Extracted from the legacy bot module without behavior changes."""

from html import escape

from aiogram import F
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from guardbot.bot.main_menu import build_main_menu_view, edit_main_menu, send_main_menu
from guardbot.bot.runtime import (
    callback_feedback,
    clear_legacy_user_state,
    clear_user_workflows,
    dp,
    stop_trade_check_task,
    user_states,
)
from guardbot.bot.states import BulkImportFlow
from guardbot.config import logger
from guardbot.database import AsyncSessionLocal, Mafile, User, save_user
from guardbot.security import decrypt_value
from guardbot.steam.client import probe_proxy_external_ip, redact_proxy_url
from sqlalchemy import select
import aiohttp
import asyncio

@dp.message(Command("delete_all_accs"))
async def cmd_delete_all_accs(message: Message, command: CommandObject):
    """Удалить все аккаунты: /delete_all_accs confirm"""
    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id

    if not args or args[0].lower() != "confirm":
        await message.answer(
            "⚠️ <b>ВНИМАНИЕ!</b>\n\n"
            "Эта команда <b>БЕЗВОЗВРАТНО УДАЛИТ</b> все ваши аккаунты.\n\n"
            "Для подтверждения используйте:\n"
            "<code>/delete_all_accs confirm</code>",
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

        count = len(mafiles)

        # Удаляем все аккаунты
        for mafile in mafiles:
            await session.delete(mafile)

        await session.commit()

    # Останавливаем задачи проверки трейдов
    for mafile in mafiles:
        stop_trade_check_task(mafile.id)

    await message.answer(
        f"✅ <b>Удалено {count} аккаунтов!</b>\n\n"
        f"Все ваши аккаунты Steam удалены из бота.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_start")]
            ]
        )
    )

@dp.message(Command("start"))
@dp.message(Command("help"))
@dp.message(Command("menu"))
async def cmd_start_help(message: Message, state: FSMContext | None = None):
    user = message.from_user
    if user is None:
        raise RuntimeError("Telegram user is unavailable for this message")
    await clear_user_workflows(user.id, state)

    if getattr(message.chat, "type", ChatType.PRIVATE) != ChatType.PRIVATE:
        await message.answer(
            "🔒 Главное меню с данными аккаунтов доступно только в личном чате с ботом."
        )
        return

    await save_user(
        telegram_id=user.id,
        username=user.username,
        full_name=user.full_name
    )

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.telegram_id == user.id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

    if message.bot is None:
        raise RuntimeError("Telegram bot is unavailable for this message")

    view = build_main_menu_view(
        user.full_name or user.username or "пользователь",
        mafiles,
        telegram_id=user.id,
    )
    await send_main_menu(
        message.bot,
        message.chat.id,
        view,
        message_thread_id=message.message_thread_id,
        business_connection_id=message.business_connection_id,
    )

@dp.message(Command("commands"))
async def cmd_commands(message: Message, state: FSMContext | None = None):
    """Список всех команд бота"""
    if message.from_user is None:
        raise RuntimeError("Telegram user is unavailable for this message")
    await clear_user_workflows(message.from_user.id, state)
    commands_text = (
        "📋 <b>СПИСОК ВСЕХ КОМАНД</b>\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "🖤 <b>Steam Guard и вход</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<code>/gc [аккаунт]</code> — получить Steam Guard код\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "👤 <b>Управление аккаунтами</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<code>/add</code> — добавить аккаунт\n"
        "<code>/login [аккаунт]</code> — войти в аккаунт\n"
        "<code>/login_all</code> — массовый вход во все аккаунты\n"
        "<code>/tokens</code> — проверить статус токенов\n"
        "<code>/accounts</code> — список аккаунтов\n"
        "<code>/acc [аккаунт]</code> — детали аккаунта\n"
        "<code>/settings [аккаунт]</code> — настройки аккаунта\n"
        "<code>/delete [аккаунт]</code> — удалить аккаунт\n"
        "<code>/delete_all_accs [аккаунт]</code> — удалить все аккаунты\n"
        "<code>/note [акк] [текст]</code> — заметка к аккаунту\n"
        "<code>/steamid [аккаунт]</code> — SteamID аккаунта\n"
        "<code>/proxy [аккаунт]</code> — прокси аккаунта\n"
        "<code>/tradelink [аккаунт]</code> — трейд-ссылка\n\n"


        "━━━━━━━━━━━━━━━━━━━━\n"
        "📂 <b>Группы</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<code>/groups</code> — список групп\n"
        "<code>/creategroup [имя]</code> — создать группу\n"
        "<code>/delgroup [имя]</code> — удалить группу\n"
        "<code>/showgroup [имя]</code> — аккаунты в группе\n"
        "<code>/expportgroup [имя]</code> — экспортировать mafile группы\n"
        "<code>/add_to_group [акк] [группа]</code> — добавить в группу\n"
        "<code>/remove_from_group [акк]</code> — убрать из группы\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔄 <b>Трейды и инвентарь</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<code>/trades [аккаунт]</code> — меню трейдов\n"
        "<code>/confirmations [аккаунт]</code> — подтверждения\n"
        "<code>/confirmall [аккаунт]</code> — подтвердить всё\n"
        "<code>/inventory [аккаунт]</code> — инвентарь\n"
        "<code>/notify_on [акк] [мин]</code> — уведомления о трейдах\n"
        "<code>/notify_off [аккаунт]</code> — выкл. уведомления\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "🌐 <b>Прокси</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<code>/set_proxy [url]</code> — общий прокси\n"
        "<code>/set_account_proxy [акк] [url]</code> — уникальный прокси\n"
        "<code>/remove_proxy</code> — удалить общий прокси\n"
        "<code>/remove_account_proxy [акк]</code> — удалить уникальный\n"
        "<code>/my_proxy</code> — посмотреть прокси\n"
        "<code>/test_proxy [акк]</code> — проверить прокси\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "📦 <b>Импорт/Экспорт</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<code>/bulk_import</code> — массовый импорт текстом\n"
        "<code>/import_mafile</code> — импорт .mafile\n"
        "<code>/export [акк]</code> — экспорт mafile\n"
        "<code>/export_all</code> — экспорт всех в ZIP\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔒 <b>Безопасность бота</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<code>/set_password [пароль]</code> — установить пароль\n"
        "<code>/lock</code> — заблокировать бота\n"
        "<code>/unlock [пароль]</code> — разблокировать\n"
        "<code>/set_lock_timeout [мин]</code> — автоблокировка\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "ℹ️ <b>Прочее</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<code>/start</code> — главное меню\n"
        "<code>/commands</code> — этот список\n"
        "<code>/cancel</code> — отменить действие\n\n"

        "━━━━━━━━━━━━━━━━━━━━\n"
        "📎 <b>Файлы (отправить в чат)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "<code>.mafile</code> — ключи Steam Guard\n"
        "<code>.json</code> — JSON с данными\n"
        "<code>.zip</code> — архив с mafile\n"
        "<code>.txt</code> — список log:pass\n"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_start")]
        ]
    )

    await message.answer(commands_text, parse_mode="HTML", reply_markup=keyboard)

@dp.message(Command("remove_proxy"))
@dp.message(Command("delete_proxy"))
async def cmd_remove_general_proxy(message: Message):
    """Удаление общего прокси"""
    telegram_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if not user or not user.general_proxy:
            await message.answer("❌ У вас нет общего прокси")
            return

        # Проверяем, есть ли у пользователя аккаунты с уникальными прокси
        stmt = select(Mafile).where(
            Mafile.telegram_id == telegram_id,
            Mafile.unique_proxy == True,
            Mafile.proxy.isnot(None)
        )
        result = await session.execute(stmt)
        has_unique_proxy = result.scalar_one_or_none() is not None

        user.general_proxy = None
        await session.commit()

        if has_unique_proxy:
            warning = "\n\n⚠️ <b>Внимание:</b> У вас есть аккаунты с уникальными прокси, они продолжат работать."
        else:
            warning = "\n\n⚠️ <b>Внимание:</b> Теперь у вас нет ни одного прокси!\nВы сможете только получать Steam Guard коды. Все остальные функции будут недоступны."

        await message.answer(
            f"✅ <b>Общий прокси удален!</b>{warning}",
            parse_mode="HTML"
        )

@dp.message(Command("import_bulk"))
@dp.message(Command("mass_import"))
@dp.message(Command("bulk_import"))
async def cmd_bulk_import(message: Message, state: FSMContext):
    """Массовый импорт аккаунтов"""
    if message.from_user is None:
        raise RuntimeError("Telegram user is unavailable for this message")
    if message.chat.type != ChatType.PRIVATE:
        await message.answer(
            "❌ Данные аккаунтов можно вводить только в личном чате с ботом."
        )
        return

    try:
        await clear_user_workflows(message.from_user.id, state)
        await state.set_state(BulkImportFlow.waiting_credentials)
        await state.set_data({})
        await message.answer(
            "📦 <b>МАССОВЫЙ ИМПОРТ АККАУНТОВ</b>\n\n"
            "Отправьте список аккаунтов в формате:\n"
            "<code>login1:password1</code>\n"
            "<code>login2:password2</code>\n\n"
            "Для отмены используйте /cancel",
            parse_mode="HTML"
        )
    except (Exception, asyncio.CancelledError):
        await clear_user_workflows(message.from_user.id, state)
        raise


@dp.callback_query(F.data == "bulk_import_menu")
async def callback_bulk_import_menu(callback: CallbackQuery, state: FSMContext):
    """Кнопка массового импорта из меню"""
    await callback_feedback(callback)
    chat = getattr(callback.message, "chat", None)
    if getattr(chat, "type", None) != ChatType.PRIVATE:
        await callback_feedback(
            callback,
            "❌ Данные аккаунтов можно вводить только в личном чате с ботом.",
            show_alert=True,
        )
        return

    try:
        await clear_user_workflows(callback.from_user.id, state)
        await state.set_state(BulkImportFlow.waiting_credentials)
        await state.set_data({})
        await callback.message.edit_text(
            "📦 <b>МАССОВЫЙ ИМПОРТ АККАУНТОВ</b>\n\n"
            "Отправьте список аккаунтов в формате:\n"
            "<code>login1:password1</code>\n"
            "<code>login2:password2</code>\n\n"
            "Для отмены нажмите кнопку ниже:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_state_to_start")]
                ]
            )
        )
    except (Exception, asyncio.CancelledError):
        await clear_user_workflows(callback.from_user.id, state)
        raise

@dp.callback_query(F.data == "cancel_state_to_start")
async def callback_cancel_state_to_start(
    callback: CallbackQuery,
    state: FSMContext | None = None,
):
    """Отмена текущего действия и возврат в главное меню"""
    user_id = callback.from_user.id
    await clear_user_workflows(user_id, state)

    chat = getattr(callback.message, "chat", None)
    if getattr(chat, "type", ChatType.PRIVATE) != ChatType.PRIVATE:
        await callback_feedback(
            callback,
            "🔒 Главное меню доступно только в личном чате с ботом.",
            show_alert=True,
        )
        return
    await callback_feedback(callback, "❌ Действие отменено")

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

@dp.message(Command("test_proxy"))
async def cmd_test_proxy(message: Message, command: CommandObject):
    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id

    if not args:
        await message.answer(
            "❌ <b>Использование:</b>\n\n"
            "<code>/test_proxy [account]</code> - проверить прокси аккаунта\n"
            "<code>/test_proxy general</code> - проверить общий прокси\n\n"
            "<b>Примеры:</b>\n"
            "<code>/test_proxy 1</code> - первый аккаунт\n"
            "<code>/test_proxy mylogin</code> - по логину\n"
            "<code>/test_proxy general</code> - общий прокси",
            parse_mode="HTML"
        )
        return

    account_input = args[0]
    try:
        # Проверка общего прокси
        if account_input.lower() == 'general':
            async with AsyncSessionLocal() as session:
                stmt = select(User).where(User.telegram_id == telegram_id)
                result = await session.execute(stmt)
                user = result.scalar_one_or_none()

                if not user or not user.general_proxy:
                    await message.answer(
                        "❌ <b>Общий прокси не настроен!</b>\n\n"
                        "Настройте прокси командой:\n"
                        "<code>/set_proxy http://user:pass@host:port</code>",
                        parse_mode="HTML"
                    )
                    return

                proxy = decrypt_value(user.general_proxy)
                if not proxy:
                    raise ValueError("Сохранённый прокси пуст")
                status_msg = await message.answer("🔄 <b>Проверяю общий прокси...</b>", parse_mode="HTML")

                external_ip = await probe_proxy_external_ip(proxy)

                await status_msg.edit_text(
                    f"✅ <b>Общий прокси работает!</b>\n\n"
                    f"🌐 <b>Прокси:</b> <code>{escape(str(redact_proxy_url(proxy)))}</code>\n"
                    f"📍 <b>Внешний IP:</b> <code>{escape(str(external_ip))}</code>\n\n"
                    f"Теперь можете использовать /login",
                    parse_mode="HTML"
                )
            return

        # Проверка прокси конкретного аккаунта
        async with AsyncSessionLocal() as session:
            stmt = select(Mafile).where(Mafile.telegram_id == telegram_id)
            result = await session.execute(stmt)
            mafiles = result.scalars().all()

            if not mafiles:
                await message.answer("❌ У вас нет аккаунтов")
                return

            mafile = None
            if account_input.isdigit():
                idx = int(account_input) - 1
                if 0 <= idx < len(mafiles):
                    mafile = mafiles[idx]
            else:
                mafile = next((mf for mf in mafiles if mf.account_name == account_input), None)

            if not mafile:
                await message.answer(f"❌ Аккаунт <b>{escape(str(account_input))}</b> не найден", parse_mode="HTML")
                return

            status_msg = await message.answer("🔄 <b>Проверяю прокси соединение...</b>", parse_mode="HTML")

            proxy = None
            proxy_type = "Общий"

            if mafile.unique_proxy and mafile.proxy:
                proxy = decrypt_value(mafile.proxy)
                proxy_type = "Уникальный"
            else:
                stmt = select(User).where(User.telegram_id == telegram_id)
                result = await session.execute(stmt)
                user = result.scalar_one_or_none()
                if user and user.general_proxy:
                    proxy = decrypt_value(user.general_proxy)

            if not proxy:
                await status_msg.edit_text(
                    f"❌ <b>Прокси не настроен для аккаунта {escape(str(mafile.account_name))}!</b>\n\n"
                    "Настройте прокси:\n"
                    "<code>/set_proxy http://user:pass@host:port</code> - общий\n"
                    f"<code>/set_account_proxy {escape(str(mafile.account_name))} http://user:pass@host:port</code> - уникальный",
                    parse_mode="HTML"
                )
                return

            external_ip = await probe_proxy_external_ip(proxy)

            await status_msg.edit_text(
                f"✅ <b>Прокси работает!</b>\n\n"
                f"📱 <b>Аккаунт:</b> <code>{escape(str(mafile.account_name))}</code>\n"
                f"🔧 <b>Тип прокси:</b> {proxy_type}\n"
                f"🌐 <b>Прокси:</b> <code>{escape(str(redact_proxy_url(proxy)))}</code>\n"
                f"📍 <b>Внешний IP:</b> <code>{escape(str(external_ip))}</code>\n\n"
                f"Теперь можете использовать /login {escape(str(mafile.account_name))}",
                parse_mode="HTML"
            )

    except aiohttp.ClientProxyConnectionError as e:
        await status_msg.edit_text(
            f"❌ <b>Ошибка подключения к прокси!</b>\n"
            f"<code>{type(e).__name__}</code>\n\n"
            f"<b>Прокси:</b> <code>{escape(str(redact_proxy_url(proxy)))}</code>",
            parse_mode="HTML"
        )
    except aiohttp.ClientResponseError as e:
        await status_msg.edit_text(
            f"❌ <b>Ошибка ответа от прокси (код {e.status})!</b>\n"
            f"<b>Прокси:</b> <code>{escape(str(redact_proxy_url(proxy)))}</code>",
            parse_mode="HTML"
        )
    except asyncio.TimeoutError:
        await status_msg.edit_text(
            f"❌ <b>Таймаут соединения!</b>\n\n"
            f"<b>Прокси:</b> <code>{escape(str(redact_proxy_url(proxy)))}</code>",
            parse_mode="HTML"
        )
    except Exception as e:
        await status_msg.edit_text(
            f"❌ <b>Ошибка соединения:</b>\n"
            f"<code>{type(e).__name__}</code>\n\n"
            f"<b>Прокси:</b> <code>{escape(str(redact_proxy_url(proxy)))}</code>",
            parse_mode="HTML"
        )