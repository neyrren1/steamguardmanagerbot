"""Fallback message handler, registered last.

Every conversational step has its own typed-FSM handler registered earlier;
this handler only routes stateless archive/account-list uploads and shows a
short command hint for ordinary text.
"""

from aiogram.enums import ChatType
from aiogram import Router
from aiogram.types import Message
from guardbot.bot.handlers.bulk_import import (
    process_txt_accounts,
    process_zip_archive,
)

router = Router(name=__name__)


UNKNOWN_COMMAND_IN_FLOW_HINT = (
    "❌ Это не команда бота, а ввод, начинающийся с «/», здесь не принимается.\n"
    "Отправьте значение ещё раз или /cancel — отменить."
)


@router.message()
async def handle_messages(message: Message, raw_state: str | None = None):
    """Handle messages that no workflow-specific handler consumed."""
    user_id = message.from_user.id

    # Unmatched commands (unknown, or for another bot) are never treated as
    # input, including a command written in a document caption. Commands for
    # another bot never get here: LockCheckMiddleware drops them.
    if (message.text or message.caption or "").startswith("/"):
        # Inside an input flow the user is waiting for a reply; the flow's
        # own handlers ignore slash-prefixed text, so say why nothing
        # happened. Stay silent in groups and outside flows.
        if raw_state and message.chat.type == ChatType.PRIVATE:
            await message.answer(UNKNOWN_COMMAND_IN_FLOW_HINT)
        return

    if message.document:
        file_name_lower = (message.document.file_name or "").lower()

        # Сначала проверяем ZIP
        if file_name_lower.endswith('.zip'):
            await process_zip_archive(message, user_id)
            return

        # Потом TXT с аккаунтами
        if file_name_lower.endswith('.txt'):
            await process_txt_accounts(message, user_id)
            return
        return

    if message.text:
        await message.answer(
            "💡 Используйте команды для управления ботом:\n"
            "/start - главное меню\n"
            "/commands - список всех команд\n"
            "/accounts - список аккаунтов\n"
            "/add - добавить аккаунт\n"
            "/gc - получить Steam Guard код"
        )