"""Add a new Steam account by login credentials (AccountAddFlow)."""

from contextlib import suppress
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from guardbot.bot.runtime import callback_feedback, clear_user_workflows
from guardbot.bot.states import AccountAddFlow
from guardbot.config import logger
from guardbot.database import AsyncSessionLocal, Mafile
from guardbot.security import encrypt_value
from sqlalchemy import func, select
from guardbot.bot.handlers.account_list import show_accounts_page

router = Router(name=__name__)


async def _start_account_add(state: FSMContext, user_id: int) -> None:
    """Start the typed flow after closing any competing workflow."""
    await clear_user_workflows(user_id, state)
    await state.set_state(AccountAddFlow.waiting_credentials)


@router.callback_query(F.data == "add_account")
async def callback_add_account(callback: CallbackQuery, state: FSMContext):
    if not callback.message or callback.message.chat.type != ChatType.PRIVATE:
        await callback_feedback(
            callback,
            "❌ Добавление аккаунта доступно только в личном чате с ботом.",
            show_alert=True,
        )
        return

    add_text = (
        "➕ <b>ДОБАВЛЕНИЕ АККАУНТА</b>\n\n"
        "Отправьте данные в формате:\n"
        "<code>login:password</code>\n\n"
        "Например: <code>mylogin:mypassword</code>"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_state_to_accounts")]  # 🔥 Изменено
        ]
    )

    await callback_feedback(callback)
    await _start_account_add(state, callback.from_user.id)
    try:
        await callback.message.edit_text(
            add_text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception:
        await state.clear()
        raise


@router.callback_query(F.data == "cancel_state_to_accounts")
async def callback_cancel_state_to_accounts(
    callback: CallbackQuery,
    state: FSMContext,
):
    """Отмена добавления аккаунта и возврат к списку"""
    user_id = callback.from_user.id
    await clear_user_workflows(user_id, state)

    await callback_feedback(callback, "❌ Добавление отменено")
    await show_accounts_page(callback, page=0)


@router.message(Command("add_steam"))
@router.message(Command("add"))
@router.message(Command("new"))
async def cmd_add_steam(
    message: Message,
    state: FSMContext,
    **kwargs,
):
    if message.chat.type != ChatType.PRIVATE:
        await message.answer(
            "❌ Добавление аккаунта доступно только в личном чате с ботом."
        )
        return

    await _start_account_add(state, message.from_user.id)
    try:
        await message.answer(
            "📝 Для добавления Steam аккаунта, отправьте данные в формате:\n\n"
            "<code>account_name:password</code>\n\n"
            "Например: <code>mylogin:mypassword</code>",
            parse_mode="HTML",
        )
    except Exception:
        await state.clear()
        raise


@router.message(
    AccountAddFlow.waiting_credentials,
    F.text,
    ~F.text.startswith("/"),
)
async def receive_account_credentials(
    message: Message,
    state: FSMContext,
):
    """Validate a login/password pair while keeping invalid input retryable."""
    with suppress(TelegramAPIError):
        await message.delete()

    if message.chat.type != ChatType.PRIVATE:
        await message.answer(
            "❌ Данные аккаунта можно вводить только в личном чате с ботом."
        )
        return

    text = (message.text or "").strip()
    if ":" not in text:
        await message.answer(
            "❌ Неверный формат. Используйте: account_name:password"
        )
        return

    account_name, password = text.split(":", 1)
    account_name = account_name.strip()
    password = password.strip()
    if not account_name or not password:
        await message.answer(
            "❌ Неверный формат. Используйте: account_name:password"
        )
        return

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(Mafile).where(
                    Mafile.telegram_id == message.from_user.id,
                    Mafile.account_name == account_name,
                )
            )
            existing = result.scalar_one_or_none()
            if existing:
                mafile_id = None
            else:
                max_order_result = await session.execute(
                    select(func.max(Mafile.sort_order)).where(
                        Mafile.telegram_id == message.from_user.id
                    )
                )
                max_order = max_order_result.scalar() or 0

                mafile = Mafile(
                    telegram_id=message.from_user.id,
                    account_name=account_name,
                    password=encrypt_value(password),
                    sort_order=max_order + 1,
                )
                session.add(mafile)
                await session.commit()
                mafile_id = mafile.id
    except Exception:
        await state.clear()
        logger.exception(
            "Failed to add Steam account for Telegram user %s",
            message.from_user.id,
        )
        await message.answer("❌ Ошибка при добавлении аккаунта")
        return

    await state.clear()
    if mafile_id is None:
        await message.answer("❌ Аккаунт с таким именем уже существует")
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📄 Добавить mafile",
                    callback_data=f"import_mafile_for_{mafile_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔑 Выполнить вход",
                    callback_data=f"action_login_{mafile_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 К списку аккаунтов",
                    callback_data="my_accounts",
                )
            ],
        ]
    )
    await message.answer(
        f"✅ Аккаунт {account_name} добавлен!\n\n"
        "Теперь вы можете:\n"
        "• Добавить mafile для Steam Guard кодов\n"
        "• Выполнить вход для получения токенов",
        reply_markup=keyboard,
    )
