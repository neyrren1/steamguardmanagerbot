"""Per-account unique proxy: prompt, input, removal and connectivity test."""

import asyncio
from html import escape
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from guardbot.bot.callback_validation import validated_callback_data
from guardbot.bot.filters import NonCommandInputFilter
from guardbot.bot.runtime import (
    callback_feedback,
    clear_user_workflows,
    delete_secret_message_or_abort,
)
from guardbot.bot.states import ProxyFlow
from guardbot.config import logger
from guardbot.database import AsyncSessionLocal, User
from guardbot.security import decrypt_value, encrypt_value
from guardbot.services.ownership import get_owned_mafile
from guardbot.steam.client import (
    parse_proxy_string,
    probe_proxy_external_ip,
    redact_proxy_url,
)
from sqlalchemy import select
from guardbot.bot.handlers.callback_ids import (
    _MAX_DATABASE_INTEGER,
    _parse_positive_callback_id,
)
from guardbot.bot.handlers.account_settings import update_account_settings_message

router = Router(name=__name__)


async def _start_unique_proxy_prompt(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    prefix: str,
    include_current: bool,
) -> None:
    if not callback.message or callback.message.chat.type != ChatType.PRIVATE:
        await callback_feedback(
            callback,
            "❌ Ввод прокси доступен только в личном чате с ботом.",
            show_alert=True,
        )
        return

    mafile_id = _parse_positive_callback_id(callback.data, prefix)
    if mafile_id is None:
        await callback_feedback(callback, "Некорректный запрос", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return
        safe_account_name = escape(mafile.account_name)
        decrypted_proxy = (
            decrypt_value(mafile.proxy)
            if include_current and mafile.proxy
            else None
        )
        current_proxy = (
            escape(redact_proxy_url(decrypted_proxy))
            if decrypted_proxy
            else "не установлен"
        )

    await callback_feedback(callback)
    await clear_user_workflows(callback.from_user.id, state)
    try:
        await state.set_state(ProxyFlow.waiting_proxy)
        await state.set_data({"mafile_id": mafile_id})
        heading = (
            "🔒 <b>НАСТРОЙКА УНИКАЛЬНОГО ПРОКСИ</b>"
            if include_current
            else "🔒 <b>Установка уникального прокси</b>"
        )
        current_line = (
            f"🌐 Текущий прокси: <code>{current_proxy}</code>\n\n"
            if include_current
            else ""
        )
        remove_line = (
            "<b>Для удаления прокси отправьте точку:</b> <code>.</code>\n\n"
            if include_current
            else ""
        )
        await callback.message.edit_text(
            f"{heading}\n\n"
            f"📱 Аккаунт: <code>{safe_account_name}</code>\n"
            f"{current_line}"
            "<b>Отправьте новый прокси в формате:</b>\n"
            "<code>protocol://username:password@host:port</code>\n\n"
            "<b>Примеры:</b>\n"
            "• <code>http://user:pass@proxy.example.com:8080</code>\n"
            "• <code>socks5://user:pass@proxy.example.com:1080</code>\n\n"
            f"{remove_line}"
            "Для отмены нажмите кнопку ниже:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="❌ Отмена",
                            callback_data=f"cancel_state_to_settings_{mafile_id}",
                        )
                    ]
                ]
            ),
        )
    except (Exception, asyncio.CancelledError):
        await clear_user_workflows(callback.from_user.id, state)
        raise


@router.callback_query(F.data.startswith("change_unique_proxy_"))
async def callback_change_unique_proxy(
    callback: CallbackQuery,
    state: FSMContext,
):
    """Запрос на изменение/добавление уникального прокси."""
    await _start_unique_proxy_prompt(
        callback,
        state,
        prefix="change_unique_proxy_",
        include_current=True,
    )


@router.callback_query(F.data.startswith("cancel_state_to_settings_"))
async def callback_cancel_state_to_settings(
    callback: CallbackQuery,
    state: FSMContext,
):
    """Отмена текущего действия и возврат в настройки аккаунта"""
    user_id = callback.from_user.id
    await clear_user_workflows(user_id, state)
    logger.info(f"Cleared workflows for user {user_id}")

    mafile_id = _parse_positive_callback_id(
        callback.data,
        "cancel_state_to_settings_",
    )
    if mafile_id is None:
        await callback_feedback(callback, "❌ Действие отменено")
        return

    await callback_feedback(callback, "❌ Действие отменено")

    # Возвращаемся в настройки аккаунта
    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback.message.edit_text("❌ Аккаунт не найден")
            return

        await update_account_settings_message(callback.message, mafile, session)


@router.callback_query(F.data.startswith("set_unique_proxy_"))
async def callback_set_unique_proxy(
    callback: CallbackQuery,
    state: FSMContext,
):
    """Запрос на установку уникального прокси."""
    await _start_unique_proxy_prompt(
        callback,
        state,
        prefix="set_unique_proxy_",
        include_current=False,
    )


@router.message(ProxyFlow.waiting_proxy, NonCommandInputFilter())
async def receive_unique_proxy(message: Message, state: FSMContext) -> None:
    """Persist an account proxy without ever placing credentials in FSM."""
    if message.chat.type != ChatType.PRIVATE:
        return
    if not await delete_secret_message_or_abort(message):
        return

    data = await state.get_data()
    mafile_id = data.get("mafile_id")
    if (
        set(data) != {"mafile_id"}
        or type(mafile_id) is not int
        or mafile_id <= 0
        or mafile_id > _MAX_DATABASE_INTEGER
    ):
        await clear_user_workflows(message.from_user.id, state)
        await message.answer("❌ Данные настройки прокси повреждены. Начните заново.")
        return

    proxy_input = (message.text or "").strip()
    if not proxy_input:
        await message.answer("❌ Отправьте адрес прокси или точку для удаления.")
        return

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, message.from_user.id)
        if not mafile:
            await clear_user_workflows(message.from_user.id, state)
            await message.answer("❌ Аккаунт не найден")
            return

        safe_account_name = escape(mafile.account_name)
        if proxy_input == ".":
            mafile.proxy = None
            mafile.unique_proxy = False
            await session.commit()
            await clear_user_workflows(message.from_user.id, state)

            settings_msg = await message.answer(
                "⚙️ <b>Загружаю настройки...</b>",
                parse_mode="HTML",
            )
            await update_account_settings_message(settings_msg, mafile, session)
            await message.answer(
                "✅ <b>Уникальный прокси удален!</b>\n\n"
                f"📱 Аккаунт: <code>{safe_account_name}</code>\n"
                "Теперь используется общий прокси.",
                parse_mode="HTML",
            )
            return

        try:
            proxy_url = parse_proxy_string(proxy_input)
        except ValueError:
            await message.answer(
                "❌ <b>Некорректный или небезопасный адрес прокси.</b>\n\n"
                "Попробуйте снова или нажмите кнопку ниже:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🔄 Попробовать снова",
                                callback_data=f"change_unique_proxy_{mafile.id}",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                text="❌ Отмена",
                                callback_data=f"cancel_state_to_settings_{mafile.id}",
                            )
                        ],
                    ]
                ),
            )
            return

        old_proxy = mafile.proxy
        mafile.proxy = encrypt_value(proxy_url)
        await session.commit()
        await clear_user_workflows(message.from_user.id, state)

        settings_msg = await message.answer(
            "⚙️ <b>Загружаю настройки...</b>",
            parse_mode="HTML",
        )
        await update_account_settings_message(settings_msg, mafile, session)
        action = "изменен" if old_proxy else "добавлен"
        await message.answer(
            f"✅ <b>Уникальный прокси {action}!</b>\n\n"
            f"📱 Аккаунт: <code>{safe_account_name}</code>\n"
            f"🌐 Прокси: <code>{escape(redact_proxy_url(proxy_url))}</code>\n\n"
            "<i>Нажмите кнопку ниже, чтобы включить уникальный прокси</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🟢 Включить уникальный прокси",
                            callback_data=f"toggle_unique_proxy_{mafile.id}_on",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text="👤 К настройкам аккаунта",
                            callback_data=f"account_settings_{mafile.id}",
                        )
                    ],
                ]
            ),
        )


@router.callback_query(F.data.startswith("remove_account_proxy_"))
@validated_callback_data(r"remove_account_proxy_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_remove_account_proxy(callback: CallbackQuery):
    """Удаление уникального прокси с аккаунта"""
    mafile_id = int(callback.data.split("_")[3])

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        mafile.unique_proxy = False
        mafile.proxy = None
        await session.commit()

        await callback_feedback(callback, "✅ Уникальный прокси удален, используется общий", show_alert=True)
        await update_account_settings_message(callback.message, mafile, session)


@router.callback_query(F.data.startswith("test_account_proxy_"))
@validated_callback_data(r"test_account_proxy_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_test_account_proxy(callback: CallbackQuery):
    """Тестирование прокси аккаунта"""
    mafile_id = int(callback.data.split("_")[3])

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        # 🔥 Определяем и расшифровываем прокси
        proxy = None
        proxy_type = "Общий"

        if mafile.unique_proxy and mafile.proxy:
            proxy = decrypt_value(mafile.proxy)
            proxy_type = "Уникальный"
        else:
            stmt = select(User).where(User.telegram_id == mafile.telegram_id)
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()
            if user and user.general_proxy:
                proxy = decrypt_value(user.general_proxy)
            else:
                await callback_feedback(callback, "❌ Прокси не настроен!", show_alert=True)
                return

        if not proxy:
            await callback_feedback(callback, "❌ Прокси повреждён или пуст!", show_alert=True)
            return

        await callback_feedback(callback, "🔄 Тестирую прокси...")

        status_msg = await callback.message.answer(
            f"🔄 <b>Тестирую {proxy_type.lower()} прокси...</b>",
            parse_mode="HTML"
        )

        try:
            external_ip = await probe_proxy_external_ip(proxy)
            await status_msg.edit_text(
                f"✅ <b>{proxy_type} прокси работает!</b>\n\n"
                f"📱 Аккаунт: <code>{escape(mafile.account_name)}</code>\n"
                f"🌐 Прокси: <code>{escape(redact_proxy_url(proxy))}</code>\n"
                f"📍 Внешний IP: <code>{escape(external_ip)}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(
                            text="◀️ Назад",
                            callback_data=f"account_settings_{mafile.id}"
                        )
                    ]]
                )
            )

        except Exception as e:
            await status_msg.edit_text(
                f"❌ <b>Ошибка прокси:</b>\n\n<code>{type(e).__name__}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(
                            text="◀️ Назад",
                            callback_data=f"account_settings_{mafile.id}"
                        )
                    ]]
                )
            )
