"""Extracted from the legacy bot module without behavior changes."""

from html import escape

from contextlib import suppress

from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from datetime import datetime
from guardbot.bot.handlers.accounts import perform_login_full, update_account_settings_message
from guardbot.bot.handlers.bulk_import import handle_mafile_for_account, process_steam_code, process_txt_accounts, process_zip_archive
from guardbot.bot.handlers.common import cmd_start_help
from guardbot.bot.handlers.imports import process_single_mafile
from guardbot.bot.runtime import (
    bot,
    clear_legacy_user_state,
    dp,
    user_states,
)
from guardbot.config import logger
from guardbot.database import AsyncSessionLocal, Mafile
from guardbot.security import EncryptionError, decrypt_value, encrypt_value
from guardbot.services.ownership import get_owned_group, get_owned_mafile
from guardbot.steam.client import AsyncSteamMobile, generate_device_id
from sqlalchemy import func, select, text
import io
import json

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

    elif state == "waiting_steam_code_manual":
        # Обработка кода введенного вручную
        await process_steam_code(message, state_data, user_id)

    elif state == "waiting_email_code":
        # Обработка кода с почты
        await process_steam_code(message, state_data, user_id)
    # ========== СОСТОЯНИЕ: ОЖИДАНИЕ ПАРОЛЯ ДЛЯ ВХОДА ==========
    elif state == "waiting_password_for_login":
        password = message.text.strip()
        mafile_id = state_data.get("mafile_id")
        account_name = state_data.get("account_name")

        try:
            await message.delete()
        except:
            pass

        async with AsyncSessionLocal() as session:
            mafile = await get_owned_mafile(session, mafile_id, message.from_user.id)

            if not mafile:
                await message.answer("❌ Аккаунт не найден")
                await clear_legacy_user_state(user_id)
                return

            # 🔥 Шифруем пароль перед сохранением
            mafile.password = encrypt_value(password)
            await session.commit()

            # Передаем управление новому login challenge без последующей
            # очистки состояния, которое создаст perform_login_full().
            await clear_legacy_user_state(user_id)

            await message.answer(
                f"✅ <b>Пароль сохранен!</b>\n\n"
                f"Пробую войти в аккаунт...",
                parse_mode="HTML"
            )

            # Пробуем войти
            await perform_login_full(message, mafile, session)

    # ========== СОСТОЯНИЕ: ОЖИДАНИЕ ПАРОЛЯ ПРИ ИМПОРТЕ MAFILE ==========
    elif state == "waiting_password_for_mafile":
        password = message.text.strip()
        encrypted_mafile_data = state_data.get("encrypted_mafile_data")

        if not encrypted_mafile_data:
            await message.answer("❌ Данные mafile потеряны. Попробуйте импортировать заново.")
            await clear_legacy_user_state(user_id)
            return

        try:
            mafile_data = json.loads(decrypt_value(encrypted_mafile_data) or "")
        except (EncryptionError, TypeError, ValueError, json.JSONDecodeError):
            await message.answer("❌ Данные mafile повреждены. Импортируйте файл заново.")
            await clear_legacy_user_state(user_id)
            return

        try:
            # Удаляем сообщение с паролем для безопасности
            try:
                await message.delete()
            except:
                pass

            async with AsyncSessionLocal() as session:
                # Проверяем, существует ли уже такой аккаунт
                stmt = select(Mafile).where(
                    Mafile.telegram_id == user_id,
                    Mafile.account_name == mafile_data['account_name']
                )
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()

                if existing:
                    await message.answer(
                        f"❌ Аккаунт <b>{escape(str(mafile_data['account_name']))}</b> уже существует!\n"
                        f"Используйте /accounts для просмотра или /delete для удаления.",
                        parse_mode="HTML"
                    )
                    await clear_legacy_user_state(user_id)
                    return

                stmt = select(func.max(Mafile.sort_order)).where(Mafile.telegram_id == user_id)
                result = await session.execute(stmt)
                max_order = result.scalar() or 0

                # 🔥 Создаем новый аккаунт с шифрованием всех чувствительных данных
                mafile = Mafile(
                    telegram_id=user_id,
                    account_name=mafile_data['account_name'],
                    password=encrypt_value(password),
                    shared_secret=encrypt_value(mafile_data.get('shared_secret')),
                    identity_secret=encrypt_value(mafile_data.get('identity_secret')),
                    secret_1=encrypt_value(mafile_data.get('secret_1')),
                    device_id=encrypt_value(mafile_data.get('device_id')),
                    serial_number=encrypt_value(mafile_data.get('serial_number')),
                    revocation_code=encrypt_value(mafile_data.get('revocation_code')),
                    token_gid=encrypt_value(mafile_data.get('token_gid')),
                    uri=encrypt_value(mafile_data.get('uri')),
                    steamid=mafile_data.get('steamid'),
                    fully_enrolled=True,
                    unique_proxy=False,
                    sort_order=max_order + 1
                )

                # Шифруем токены если есть
                if 'Session' in mafile_data:
                    mafile.access_token = encrypt_value(mafile_data['Session'].get('AccessToken'))
                    mafile.refresh_token = encrypt_value(mafile_data['Session'].get('RefreshToken'))

                session.add(mafile)
                await session.commit()

                # Проверяем, генерируется ли код
                test_client = AsyncSteamMobile(mafile_data['account_name'], password)
                test_client.load_mobile({
                    'shared_secret': mafile_data.get('shared_secret'),
                    'device_id': mafile_data.get('device_id', generate_device_id()),
                    'identity_secret': mafile_data.get('identity_secret'),
                    'serial_number': mafile_data.get('serial_number'),
                    'revocation_code': mafile_data.get('revocation_code'),
                    'uri': mafile_data.get('uri'),
                    'token_gid': mafile_data.get('token_gid'),
                    'secret_1': mafile_data.get('secret_1')
                })

                try:
                    await test_client.align_time()
                    test_code = test_client.generate_steam_guard_code()

                    await message.answer(
                        f"✅ <b>Аккаунт успешно импортирован!</b>\n\n"
                        f"📱 <b>Логин:</b> <code>{escape(str(mafile_data['account_name']))}</code>\n"
                        f"🆔 <b>SteamID:</b> <code>{escape(str(mafile_data.get('steamid', 'Не указан')))}</code>\n"
                        f"🔐 <b>Тестовый код:</b> <b><code>{test_code}</code></b>\n\n"
                        f"<b>Доступные команды:</b>\n"
                        f"• <code>/gc {escape(str(mafile_data['account_name']))}</code> - получить код\n"
                        f"• <code>/login {escape(str(mafile_data['account_name']))}</code> - войти в аккаунт\n"
                        f"• <code>/accounts</code> - список всех аккаунтов\n\n"
                        f"💡 <b>Важно:</b> Если есть refresh_token в mafile, выполните вход для обновления токенов.",
                        parse_mode="HTML"
                    )

                except Exception as e:
                    await message.answer(
                        f"⚠️ Аккаунт сохранен, но есть проблема с генерацией кода:\n"
                        f"<code>{escape(str(e))}</code>\n\n"
                        f"Попробуйте выполнить вход: <code>/login {escape(str(mafile_data['account_name']))}</code>",
                        parse_mode="HTML"
                    )
                finally:
                    await test_client.close()

        except Exception as e:
            await message.answer(f"❌ Ошибка при сохранении: {str(e)}")
            logger.error(f"Error saving imported mafile for user {user_id}: {e}")
        finally:
            await clear_legacy_user_state(user_id)
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

        # Потом одиночный mafile/json
        if file_name_lower.endswith('.mafile') or file_name_lower.endswith('.json'):
            await process_single_mafile(message, user_id)
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