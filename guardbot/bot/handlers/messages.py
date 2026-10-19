"""Fallback message handler, registered last.

Every conversational step has its own typed-FSM handler registered earlier;
this handler only routes stateless archive/account-list uploads and shows a
short command hint for ordinary text.
"""

from aiogram.types import Message
from guardbot.bot.handlers.bulk_import import (
    process_txt_accounts,
    process_zip_archive,
)
from guardbot.bot.runtime import dp


@dp.message()
async def handle_messages(message: Message):
    """Handle messages that no workflow-specific handler consumed."""
    user_id = message.from_user.id

    # Unmatched commands (unknown, or for another bot) are never treated as
    # input, including a command written in a document caption.
    if (message.text or message.caption or "").startswith("/"):
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