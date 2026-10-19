"""Bot password, locking, and unlocking callback handlers."""

import asyncio

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from guardbot.bot.runtime import callback_feedback, clear_user_workflows
from guardbot.bot.states import BotPasswordFlow, UnlockFlow
from guardbot.database import AsyncSessionLocal, User

router = Router(name=__name__)


async def _reject_secret_callback_outside_private_chat(
    callback: CallbackQuery,
) -> bool:
    if callback.message and callback.message.chat.type == ChatType.PRIVATE:
        return False
    await callback_feedback(
        callback,
        "❌ Ввод секретов доступен только в личном чате с ботом.",
        show_alert=True,
    )
    return True


async def _start_bot_password_prompt(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    action: str,
    text: str,
) -> None:
    if await _reject_secret_callback_outside_private_chat(callback):
        return

    await callback_feedback(callback)
    await clear_user_workflows(callback.from_user.id, state)
    try:
        if action not in {"set", "change", "remove"}:
            raise ValueError("Unsupported bot-password action")
        await state.set_state(BotPasswordFlow.waiting_password)
        await state.set_data({"action": action})
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="❌ Отмена",
                            callback_data="cancel_password_to_settings",
                        )
                    ]
                ]
            ),
        )
    except (Exception, asyncio.CancelledError):
        await clear_user_workflows(callback.from_user.id, state)
        raise


@router.callback_query(F.data == "set_password")
async def callback_set_password(callback: CallbackQuery, state: FSMContext):
    """Запрос на установку пароля"""
    await _start_bot_password_prompt(
        callback,
        state,
        action="set",
        text=(
            "🔑 <b>УСТАНОВКА ПАРОЛЯ</b>\n\n"
            "Отправьте новый пароль для блокировки бота.\n\n"
            "<i>Минимум 8 символов. Пароль хранится как защищённый хеш.</i>\n\n"
            "Для отмены нажмите кнопку ниже:"
        ),
    )


@router.callback_query(F.data == "change_password")
async def callback_change_password(callback: CallbackQuery, state: FSMContext):
    """Запрос на изменение пароля"""
    await _start_bot_password_prompt(
        callback,
        state,
        action="change",
        text=(
            "🔑 <b>ИЗМЕНЕНИЕ ПАРОЛЯ</b>\n\n"
            "Отправьте новый пароль для блокировки бота.\n\n"
            "<i>Минимум 8 символов. Старый пароль будет заменён.</i>\n\n"
            "Для отмены нажмите кнопку ниже:"
        ),
    )


@router.callback_query(F.data == "remove_password")
async def callback_remove_password(callback: CallbackQuery, state: FSMContext):
    """Запрос на удаление пароля"""
    await _start_bot_password_prompt(
        callback,
        state,
        action="remove",
        text=(
            "🗑️ <b>УДАЛЕНИЕ ПАРОЛЯ</b>\n\n"
            "Введите текущий пароль для подтверждения удаления.\n\n"
            "Для отмены нажмите кнопку ниже:"
        ),
    )


@router.callback_query(F.data == "cancel_password_to_settings")
async def callback_cancel_password_to_settings(
    callback: CallbackQuery,
    state: FSMContext,
):
    """Clear password-entry state before returning to settings."""
    await clear_user_workflows(callback.from_user.id, state)
    await callback_feedback(callback)
    from guardbot.bot.handlers.accounts import update_settings_message

    await update_settings_message(callback.message, callback.from_user.id)


@router.callback_query(F.data == "lock_bot")
async def callback_lock_bot(callback: CallbackQuery, state: FSMContext):
    """Мгновенная блокировка бота"""
    telegram_id = callback.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if not user or not user.bot_password:
            await callback_feedback(callback, "❌ Сначала установите пароль!", show_alert=True)
            return

        user.last_activity = None  # Сбрасываем активность, что вызовет блокировку
        await session.commit()

    await callback_feedback(callback, "🔒 Бот заблокирован!")
    await clear_user_workflows(telegram_id, state)
    await callback.message.edit_text(
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


@router.callback_query(F.data == "unlock_bot")
async def callback_unlock_bot(callback: CallbackQuery, state: FSMContext):
    """Запрос пароля для разблокировки"""
    if await _reject_secret_callback_outside_private_chat(callback):
        return

    await callback_feedback(callback)
    await clear_user_workflows(callback.from_user.id, state)
    await state.set_state(UnlockFlow.waiting_password)

    try:
        await callback.message.edit_text(
            "🔓 <b>РАЗБЛОКИРОВКА</b>\n\n"
            "Отправьте пароль для разблокировки бота.",
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


@router.callback_query(F.data == "set_lock_timeout")
async def callback_set_lock_timeout(callback: CallbackQuery):
    """Настройка таймаута автоблокировки"""
    telegram_id = callback.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        current_timeout = user.lock_timeout if user and user.lock_timeout else 0

    await callback.message.edit_text(
        f"⏱ <b>АВТОБЛОКИРОВКА</b>\n\n"
        f"Текущий таймаут: <b>{current_timeout} мин</b> {'(выключено)' if current_timeout == 0 else ''}\n\n"
        f"Выберите время бездействия, после которого бот автоматически заблокируется:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"{'✅ ' if current_timeout == 5 else ''}5 минут",
                    callback_data="set_timeout_5"
                )],
                [InlineKeyboardButton(
                    text=f"{'✅ ' if current_timeout == 15 else ''}15 минут",
                    callback_data="set_timeout_15"
                )],
                [InlineKeyboardButton(
                    text=f"{'✅ ' if current_timeout == 30 else ''}30 минут",
                    callback_data="set_timeout_30"
                )],
                [InlineKeyboardButton(
                    text=f"{'✅ ' if current_timeout == 60 else ''}1 час",
                    callback_data="set_timeout_60"
                )],
                [InlineKeyboardButton(
                    text="🔓 Выключить",
                    callback_data="set_timeout_0"
                )],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="settings")]
            ]
        )
    )
    await callback_feedback(callback)
