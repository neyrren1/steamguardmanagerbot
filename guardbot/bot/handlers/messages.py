"""Extracted from the legacy bot module without behavior changes."""

from aiogram.types import Message
from guardbot.bot.handlers.bulk_import import (
    handle_mafile_for_account,
    process_steam_code,
    process_txt_accounts,
    process_zip_archive,
)
from guardbot.bot.runtime import (
    dp,
    user_states,
)

# ==================== ОБРАБОТЧИК МЕСЕДЖЕЙ ====================

@dp.message()
async def handle_messages(message: Message):
    """Обработчик всех текстовых сообщений в зависимости от состояния пользователя"""
    user_id = message.from_user.id
    state_data = user_states.get(user_id, {})
    state = state_data.get("state")

    # ========== СОСТОЯНИЕ: ОЖИДАНИЕ КОДА ПОДТВЕРЖДЕНИЯ STEAM ==========
    # Замените состояние waiting_steam_code на:
    if state == "waiting_steam_code":
        # Обработка кода когда он был сгенерирован ботом
        await process_steam_code(message, state_data, user_id)

    elif state == "waiting_email_code":
        # Обработка кода с почты
        await process_steam_code(message, state_data, user_id)
    # ========== СОСТОЯНИЕ: ОЖИДАНИЕ КОДА ИЛИ MAFILE ==========
    elif state == "waiting_steam_code_or_mafile":
        # 🔥 Удаляем сообщение (файл или код)
        try:
            await message.delete()
        except:
            pass
        if message.document:
            # Если прислали файл - обрабатываем как mafile
            await handle_mafile_for_account(message, state_data, user_id)
        else:
            # Если прислали текст - обрабатываем как код
            await process_steam_code(message, state_data, user_id)

    # ========== ОБРАБОТКА ДОКУМЕНТОВ БЕЗ СОСТОЯНИЯ ==========
    elif message.document:
        document = message.document
        file_name_lower = document.file_name.lower()

        # Сначала проверяем ZIP
        if file_name_lower.endswith('.zip'):
            await process_zip_archive(message, user_id)
            return

        # Потом TXT с аккаунтами
        if file_name_lower.endswith('.txt'):
            await process_txt_accounts(message, user_id)
            return

    # ========== НЕТ АКТИВНОГО СОСТОЯНИЯ ==========
    else:
        # Если нет активного состояния, просто игнорируем сообщение
        # или можно отправить подсказку
        if message.text and not message.text.startswith('/'):
            await message.answer(
                "💡 Используйте команды для управления ботом:\n"
                "/start - главное меню\n"
                "/commands - список всех команд\n"
                "/accounts - список аккаунтов\n"
                "/add - добавить аккаунт\n"
                "/gc - получить Steam Guard код"
            )