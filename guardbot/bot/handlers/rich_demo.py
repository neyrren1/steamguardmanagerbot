"""Handlers for the opt-in Telegram Rich Message showcase."""

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from guardbot.bot.rich_demo import (
    RICH_DEMO_CALLBACK_PREFIX,
    get_rich_demo_feedback,
    send_rich_demo,
)
from guardbot.bot.runtime import callback_feedback, clear_user_workflows

router = Router(name=__name__)


@router.message(Command("rich_demo"))
async def cmd_rich_demo(
    message: Message,
    state: FSMContext | None = None,
) -> None:
    """Send a safe UI demo without reading accounts or contacting Steam."""
    if message.from_user is None:
        raise RuntimeError("Telegram user is unavailable for this message")
    await clear_user_workflows(message.from_user.id, state)
    if message.chat.type != ChatType.PRIVATE:
        await message.answer(
            "🔒 Демонстрация Rich Message доступна только в личном чате с ботом."
        )
        return
    if message.bot is None:
        raise RuntimeError("Telegram bot is unavailable for this message")
    await send_rich_demo(
        message.bot,
        message.chat.id,
        message_thread_id=message.message_thread_id,
        business_connection_id=message.business_connection_id,
    )


@router.callback_query(F.data.startswith(RICH_DEMO_CALLBACK_PREFIX))
async def callback_rich_demo(callback: CallbackQuery) -> None:
    """Acknowledge demo callbacks without changing application state."""
    data = callback.data or ""
    action = data.removeprefix(RICH_DEMO_CALLBACK_PREFIX)
    feedback = get_rich_demo_feedback(action)
    if feedback is None:
        await callback_feedback(callback, "Неизвестное демонстрационное действие.", show_alert=True)
        return

    text, show_alert = feedback
    await callback_feedback(callback, text, show_alert=show_alert)
