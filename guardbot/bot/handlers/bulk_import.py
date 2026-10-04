"""Extracted from the legacy bot module without behavior changes."""

from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message
from datetime import datetime
from guardbot.archive_security import ArchiveSecurityError, validate_zip_entry_count
from guardbot.bot.handlers.commands import build_mafile_dict
from guardbot.bot.runtime import bot, dp, user_states
from guardbot.config import MAX_FILE_SIZE, MAX_ZIP_UNCOMPRESSED, logger
from guardbot.database import AccountGroup, AsyncSessionLocal, Mafile
from guardbot.security import decrypt_value, encrypt_value
from guardbot.services.ownership import get_owned_mafile
from guardbot.services.session_manager import SteamSessionManager
from guardbot.steam.client import AsyncSteamMobile, InvalidSteamGuardCode, generate_device_id
from sqlalchemy import func, select, text
import asyncio
import io
import json
import re
import time
import zipfile

# ==================== ФУНКЦИЯ: ОБРАБОТКА ТХТ ЛОГ:ПАСС ====================
async def process_txt_accounts(message: Message, user_id: int):
    """Обработка TXT файла с аккаунтами в формате log:pass"""
    document = message.document
    file_name = document.file_name

    # 🔥 Защита: эскейпим имя файла
    safe_file_name = file_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')

    # 🔥 Защита: проверка размера
    if document.file_size and document.file_size > MAX_FILE_SIZE:
        await message.answer(
            f"❌ <b>Файл слишком большой!</b>\n"
            f"Максимальный размер: <code>{MAX_FILE_SIZE // (1024*1024)} MB</code>",
            parse_mode="HTML"
        )
        try:
            await message.delete()
        except:
            pass
        return

    # Удаляем файл из чата
    try:
        await message.delete()
    except:
        pass

    status_msg = await message.answer(
        f"📄 <b>Обнаружен файл:</b> {safe_file_name}\n🔄 Обрабатываю аккаунты...",
        parse_mode="HTML"
    )

    try:
        # Скачиваем файл
        file_id = document.file_id
        file = await bot.get_file(file_id)
        file_path = file.file_path

        import io
        file_bytes = io.BytesIO()
        await bot.download_file(file_path, file_bytes)
        file_bytes.seek(0)

        # Пробуем разные кодировки
        try:
            content = file_bytes.read().decode('utf-8')
        except UnicodeDecodeError:
            try:
                file_bytes.seek(0)
                content = file_bytes.read().decode('cp1251')
            except:
                await status_msg.edit_text(
                    "❌ <b>Не удалось прочитать файл.</b>\n"
                    "Сохраните файл в кодировке UTF-8.",
                    parse_mode="HTML"
                )
                return

        # Разбираем строки
        lines = content.strip().split('\n')

        # 🔥 Защита: ограничение на количество строк
        if len(lines) > 10000:
            await status_msg.edit_text(
                f"❌ <b>Слишком много строк в файле!</b>\n"
                f"Найдено: {len(lines)}, максимум: 10000",
                parse_mode="HTML"
            )
            return

        # Фильтруем пустые строки и комментарии
        valid_accounts = []
        invalid_lines = []

        for idx, line in enumerate(lines, 1):
            line = line.strip()

            # Пропускаем пустые строки и комментарии
            if not line or line.startswith('#') or line.startswith('//'):
                continue

            # Проверяем формат log:pass
            if ':' in line:
                parts = line.split(':', 1)
                login = parts[0].strip()
                password = parts[1].strip()

                if login and password:
                    # 🔥 Защита: проверка длины логина и пароля
                    if len(login) > 255 or len(password) > 255:
                        invalid_lines.append(f"Строка {idx}: логин или пароль слишком длинные")
                    else:
                        valid_accounts.append((login, password))
                else:
                    invalid_lines.append(f"Строка {idx}: пустой логин или пароль")
            else:
                # Пробуем другие разделители
                if ';' in line:
                    parts = line.split(';', 1)
                    login = parts[0].strip()
                    password = parts[1].strip()
                    if login and password:
                        if len(login) > 255 or len(password) > 255:
                            invalid_lines.append(f"Строка {idx}: логин или пароль слишком длинные")
                        else:
                            valid_accounts.append((login, password))
                    else:
                        invalid_lines.append(f"Строка {idx}: неверный формат")
                elif '\t' in line:
                    parts = line.split('\t', 1)
                    login = parts[0].strip()
                    password = parts[1].strip()
                    if login and password:
                        if len(login) > 255 or len(password) > 255:
                            invalid_lines.append(f"Строка {idx}: логин или пароль слишком длинные")
                        else:
                            valid_accounts.append((login, password))
                    else:
                        invalid_lines.append(f"Строка {idx}: неверный формат")
                else:
                    invalid_lines.append(f"Строка {idx}: нет разделителя (используйте : ; или tab)")

        if not valid_accounts:
            await status_msg.edit_text(
                "❌ <b>В файле не найдено аккаунтов в формате log:pass</b>\n\n"
                f"Найдено строк с ошибками: {len(invalid_lines)}",
                parse_mode="HTML"
            )
            return

        # 🔥 Защита: ограничение на количество валидных аккаунтов
        if len(valid_accounts) > 1000:
            await status_msg.edit_text(
                f"❌ <b>Слишком много аккаунтов!</b>\n"
                f"Найдено: {len(valid_accounts)}, максимум: 1000",
                parse_mode="HTML"
            )
            return

        total = len(valid_accounts)
        added = []
        skipped = []
        errors = []

        await status_msg.edit_text(
            f"📄 <b>Файл:</b> {safe_file_name}\n"
            f"📝 Найдено аккаунтов: {total}\n"
            f"⏳ Прогресс: 0/{total}",
            parse_mode="HTML"
        )

        last_update = time.time()

        # Получаем максимальный sort_order один раз перед циклом
        async with AsyncSessionLocal() as session:
            stmt = select(func.max(Mafile.sort_order)).where(Mafile.telegram_id == user_id)
            result = await session.execute(stmt)
            max_order = result.scalar() or 0

        for idx, (account_name, password) in enumerate(valid_accounts, 1):
            try:
                async with AsyncSessionLocal() as session:
                    # Проверяем, существует ли уже такой аккаунт
                    stmt = select(Mafile).where(
                        Mafile.telegram_id == user_id,
                        Mafile.account_name == account_name
                    )
                    result = await session.execute(stmt)
                    existing = result.scalar_one_or_none()

                    if existing:
                        # Обновляем пароль существующего аккаунта
                        existing.password = encrypt_value(password)
                        existing.updated_at = datetime.utcnow()
                        await session.commit()
                        skipped.append(account_name)
                    else:
                        # Создаем новый аккаунт
                        mafile = Mafile(
                            telegram_id=user_id,
                            account_name=account_name,
                            password=encrypt_value(password),
                            unique_proxy=False,
                            sort_order=max_order + idx
                        )
                        session.add(mafile)
                        await session.commit()
                        added.append(account_name)

            except Exception as e:
                errors.append(f"{account_name}: {str(e)[:50]}")
                logger.error(f"Error adding account {account_name} from TXT: {e}")

            # Обновляем прогресс
            now = time.time()
            if now - last_update >= 3 or idx % 20 == 0 or idx == total:
                progress_text = (
                    f"📄 <b>Файл:</b> {safe_file_name}\n"
                    f"📝 Всего аккаунтов: {total}\n"
                    f"⏳ Прогресс: {idx}/{total}\n"
                    f"✅ Добавлено: {len(added)}\n"
                    f"🔄 Обновлено: {len(skipped)}\n"
                    f"❌ Ошибок: {len(errors)}"
                )
                try:
                    await status_msg.edit_text(progress_text, parse_mode="HTML")
                except:
                    pass
                last_update = now

            await asyncio.sleep(0.05)

        # Финальный отчет
        result_text = f"📊 <b>ИМПОРТ ИЗ ФАЙЛА ЗАВЕРШЕН</b>\n\n"
        result_text += f"📄 Файл: {safe_file_name}\n"
        result_text += f"📝 Всего строк: {len(lines)}\n"
        result_text += f"✅ Валидных аккаунтов: {total}\n\n"

        if added:
            result_text += f"✅ <b>Добавлено новых ({len(added)}):</b>\n"
            for acc in added[:15]:
                result_text += f"  • <code>{acc}</code>\n"
            if len(added) > 15:
                result_text += f"  <i>... и еще {len(added) - 15}</i>\n"
            result_text += "\n"

        if skipped:
            result_text += f"🔄 <b>Обновлены пароли ({len(skipped)}):</b>\n"
            for acc in skipped[:10]:
                result_text += f"  • <code>{acc}</code>\n"
            if len(skipped) > 10:
                result_text += f"  <i>... и еще {len(skipped) - 10}</i>\n"
            result_text += "\n"

        if invalid_lines:
            result_text += f"⚠️ <b>Строки с ошибками ({len(invalid_lines)}):</b>\n"
            for err in invalid_lines[:5]:
                result_text += f"  • {err}\n"
            if len(invalid_lines) > 5:
                result_text += f"  <i>... и еще {len(invalid_lines) - 5}</i>\n"
            result_text += "\n"

        if errors:
            result_text += f"❌ <b>Ошибки при сохранении ({len(errors)}):</b>\n"
            for err in errors[:5]:
                result_text += f"  • {err}\n"
            if len(errors) > 5:
                result_text += f"  <i>... и еще {len(errors) - 5}</i>\n"
            result_text += "\n"

        result_text += (
            f"<b>Итого:</b>\n"
            f"├─ Добавлено: {len(added)}\n"
            f"├─ Обновлено: {len(skipped)}\n"
            f"├─ Ошибок в строках: {len(invalid_lines)}\n"
            f"└─ Ошибок сохранения: {len(errors)}"
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="👤 Посмотреть аккаунты",
                    callback_data="my_accounts"
                )],
                [InlineKeyboardButton(
                    text="🏠 Главное меню",
                    callback_data="back_to_start"
                )]
            ]
        )

        await status_msg.edit_text(result_text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        logger.error(f"Error processing TXT file: {e}", exc_info=True)
        await status_msg.edit_text(
            f"❌ <b>Ошибка при обработке файла:</b>\n<code>{str(e)[:300]}</code>",
            parse_mode="HTML"
        )

# ==================== ФУНКЦИЯ: ОБРАБОТКА ЗИП С МАФАЙЛАМИ ====================
async def process_zip_archive(message: Message, user_id: int):
    """Обработка ZIP архива с mafile файлами"""
    document = message.document
    file_name = document.file_name

    # 🔥 Защита: эскейпим имя файла
    safe_file_name = file_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')

    # 🔥 Защита: проверка размера файла
    if document.file_size and document.file_size > MAX_FILE_SIZE:
        await message.answer(
            f"❌ <b>Файл слишком большой!</b>\n"
            f"Максимальный размер: <code>{MAX_FILE_SIZE // (1024*1024)} MB</code>",
            parse_mode="HTML"
        )
        try:
            await message.delete()
        except:
            pass
        return

    # 🔥 Удаляем сообщение с архивом
    try:
        await message.delete()
    except:
        pass

    status_msg = await message.answer(f"📦 <b>Обнаружен архив:</b> {safe_file_name}\n🔄 Начинаю обработку...", parse_mode="HTML")

    try:
        # Скачиваем архив
        file_id = document.file_id
        file = await bot.get_file(file_id)
        file_path = file.file_path

        import io
        zip_bytes = io.BytesIO()
        await bot.download_file(file_path, zip_bytes)
        zip_bytes.seek(0)

        validate_zip_entry_count(zip_bytes.getbuffer(), max_entries=500)

        # Открываем ZIP
        with zipfile.ZipFile(zip_bytes, 'r') as zip_file:
            # 🔥 Защита: проверка общего размера внутри архива
            total_uncompressed = sum(info.file_size for info in zip_file.infolist())
            if total_uncompressed > MAX_ZIP_UNCOMPRESSED:
                await status_msg.edit_text(
                    f"❌ <b>Архив слишком большой!</b>\n"
                    f"Размер внутри архива: <code>{total_uncompressed // (1024*1024)} MB</code>\n"
                    f"Максимальный: <code>{MAX_ZIP_UNCOMPRESSED // (1024*1024)} MB</code>",
                    parse_mode="HTML"
                )
                return

            # Работаем с ZipInfo, чтобы проверять размер до распаковки в память.
            archive_entries = [info for info in zip_file.infolist() if not info.is_dir()]
            mafile_files = [info for info in archive_entries if info.filename.lower().endswith('.mafile')]
            json_files = [
                info for info in archive_entries
                if info.filename.lower().endswith('.json') and info not in mafile_files
            ]
            all_files = mafile_files + json_files

            if not all_files:
                await status_msg.edit_text(
                    "❌ <b>В архиве нет .mafile или .json файлов!</b>",
                    parse_mode="HTML"
                )
                return

            # 🔥 Защита: ограничение на количество файлов
            if len(all_files) > 500:
                await status_msg.edit_text(
                    f"❌ <b>Слишком много файлов в архиве!</b>\n"
                    f"Найдено: {len(all_files)}, максимум: 500",
                    parse_mode="HTML"
                )
                return

            total = len(all_files)
            added = []
            updated = []
            errors = []

            await status_msg.edit_text(
                f"📦 <b>Обработка архива:</b> {safe_file_name}\n"
                f"📄 Найдено файлов: {total}\n"
                f"⏳ Прогресс: 0/{total}",
                parse_mode="HTML"
            )

            last_update = time.time()

            for idx, zip_info in enumerate(all_files, 1):
                file_name_in_zip = zip_info.filename
                # 🔥 Защита: эскейпим имя файла из архива
                safe_inner_name = file_name_in_zip.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')

                try:
                    if zip_info.flag_bits & 0x1:
                        errors.append(f"{safe_inner_name}: зашифрованные файлы не поддерживаются")
                        continue

                    if zip_info.file_size > MAX_FILE_SIZE:
                        errors.append(f"{safe_inner_name}: файл слишком большой, пропущен")
                        continue

                    # Читаем содержимое файла
                    file_content = zip_file.read(zip_info)

                    # 🔥 Защита: проверка размера отдельного файла в архиве
                    if len(file_content) > MAX_FILE_SIZE:
                        errors.append(f"{safe_inner_name}: файл слишком большой, пропущен")
                        continue

                    # Пробуем разные кодировки
                    try:
                        content_str = file_content.decode('utf-8')
                    except UnicodeDecodeError:
                        try:
                            content_str = file_content.decode('cp1251')
                        except:
                            errors.append(f"{safe_inner_name}: не удалось прочитать (бинарный файл?)")
                            continue

                    # Парсим JSON
                    mafile_data = json.loads(content_str)

                    account_name = mafile_data.get('account_name')
                    shared_secret = mafile_data.get('shared_secret')

                    if not account_name:
                        errors.append(f"{safe_inner_name}: не указан account_name")
                        continue

                    if not shared_secret:
                        errors.append(f"{safe_inner_name}: отсутствует shared_secret")
                        continue

                    async with AsyncSessionLocal() as session:
                        stmt = select(Mafile).where(
                            Mafile.telegram_id == user_id,
                            Mafile.account_name == account_name
                        )
                        result = await session.execute(stmt)
                        existing = result.scalar_one_or_none()

                        if existing:
                            # Обновляем существующий (шифруем)
                            existing.shared_secret = encrypt_value(shared_secret)
                            existing.identity_secret = encrypt_value(mafile_data.get('identity_secret'))
                            existing.secret_1 = encrypt_value(mafile_data.get('secret_1'))
                            existing.device_id = encrypt_value(mafile_data.get('device_id'))
                            existing.serial_number = encrypt_value(mafile_data.get('serial_number'))
                            existing.revocation_code = encrypt_value(mafile_data.get('revocation_code'))
                            existing.token_gid = encrypt_value(mafile_data.get('token_gid'))
                            existing.uri = encrypt_value(mafile_data.get('uri'))
                            existing.fully_enrolled = True
                            existing.updated_at = datetime.utcnow()

                            if 'Session' in mafile_data:
                                existing.access_token = encrypt_value(mafile_data['Session'].get('AccessToken'))
                                existing.refresh_token = encrypt_value(mafile_data['Session'].get('RefreshToken'))
                                if mafile_data['Session'].get('SteamID'):
                                    existing.steamid = int(mafile_data['Session']['SteamID'])

                            await session.commit()
                            updated.append(account_name)
                        else:
                            # Создаем новый (без пароля)
                            steamid = None
                            if 'Session' in mafile_data and mafile_data['Session'].get('SteamID'):
                                steamid = int(mafile_data['Session']['SteamID'])
                            elif 'steamid' in mafile_data:
                                try:
                                    steamid = int(mafile_data['steamid'])
                                except:
                                    pass

                            stmt = select(func.max(Mafile.sort_order)).where(Mafile.telegram_id == user_id)
                            result = await session.execute(stmt)
                            max_order = result.scalar() or 0

                            mafile = Mafile(
                                telegram_id=user_id,
                                account_name=account_name,
                                password="",
                                shared_secret=encrypt_value(shared_secret),
                                identity_secret=encrypt_value(mafile_data.get('identity_secret')),
                                secret_1=encrypt_value(mafile_data.get('secret_1')),
                                device_id=encrypt_value(mafile_data.get('device_id')),
                                serial_number=encrypt_value(mafile_data.get('serial_number')),
                                revocation_code=encrypt_value(mafile_data.get('revocation_code')),
                                token_gid=encrypt_value(mafile_data.get('token_gid')),
                                uri=encrypt_value(mafile_data.get('uri')),
                                steamid=steamid,
                                fully_enrolled=True,
                                unique_proxy=False,
                                sort_order=max_order + 1
                            )

                            if 'Session' in mafile_data:
                                mafile.access_token = encrypt_value(mafile_data['Session'].get('AccessToken'))
                                mafile.refresh_token = encrypt_value(mafile_data['Session'].get('RefreshToken'))

                            session.add(mafile)
                            await session.commit()
                            added.append(account_name)

                except json.JSONDecodeError as e:
                    errors.append(f"{safe_inner_name}: ошибка JSON - {str(e)[:50]}")
                except Exception as e:
                    errors.append(f"{safe_inner_name}: {str(e)[:80]}")
                    logger.error(f"Error processing {file_name_in_zip} from ZIP: {e}")

                # Обновляем прогресс
                now = time.time()
                if now - last_update >= 5 or idx % 10 == 0 or idx == total:
                    progress_text = (
                        f"📦 <b>Обработка архива:</b> {safe_file_name}\n"
                        f"📄 Всего файлов: {total}\n"
                        f"⏳ Прогресс: {idx}/{total}\n"
                        f"✅ Добавлено: {len(added)}\n"
                        f"🔄 Обновлено: {len(updated)}\n"
                        f"❌ Ошибок: {len(errors)}"
                    )
                    try:
                        await status_msg.edit_text(progress_text, parse_mode="HTML")
                    except:
                        pass
                    last_update = now

                await asyncio.sleep(0.1)

        # Финальный отчет
        result_text = f"📊 <b>ИМПОРТ ИЗ АРХИВА ЗАВЕРШЕН</b>\n\n"
        result_text += f"📦 Архив: {safe_file_name}\n"
        result_text += f"📄 Всего файлов: {total}\n\n"

        if added:
            result_text += f"✅ <b>Добавлено новых ({len(added)}):</b>\n"
            for acc in added[:15]:
                result_text += f"  • <code>{acc}</code>\n"
            if len(added) > 15:
                result_text += f"  <i>... и еще {len(added) - 15}</i>\n"
            result_text += "\n"

        if updated:
            result_text += f"🔄 <b>Обновлено ({len(updated)}):</b>\n"
            for acc in updated[:10]:
                result_text += f"  • <code>{acc}</code>\n"
            if len(updated) > 10:
                result_text += f"  <i>... и еще {len(updated) - 10}</i>\n"
            result_text += "\n"

        if errors:
            result_text += f"❌ <b>Ошибки ({len(errors)}):</b>\n"
            for err in errors[:10]:
                result_text += f"  • {err}\n"
            if len(errors) > 10:
                result_text += f"  <i>... и еще {len(errors) - 10}</i>\n"
            result_text += "\n"
            result_text += "<i>💡 Попробуйте добавить проблемные аккаунты по одному через /import_mafile</i>\n\n"

        result_text += (
            f"<b>Итого:</b>\n"
            f"├─ Успешно: {len(added) + len(updated)}\n"
            f"└─ Ошибок: {len(errors)}\n\n"
            f"⚠️ <b>Внимание:</b> Для добавленных аккаунтов не сохранены пароли.\n"
            f"Добавьте пароли в настройках аккаунтов."
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="👤 Посмотреть аккаунты",
                    callback_data="my_accounts"
                )],
                [InlineKeyboardButton(
                    text="🏠 Главное меню",
                    callback_data="back_to_start"
                )]
            ]
        )

        await status_msg.edit_text(result_text, parse_mode="HTML", reply_markup=keyboard)

    except zipfile.BadZipFile:
        await status_msg.edit_text(
            f"❌ <b>Файл поврежден или не является ZIP-архивом</b>",
            parse_mode="HTML"
        )
    except ArchiveSecurityError:
        await status_msg.edit_text(
            "❌ <b>Архив содержит слишком много записей или имеет неподдерживаемый формат</b>",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.error(f"Error processing ZIP archive: {e}", exc_info=True)
        await status_msg.edit_text(
            f"❌ <b>Ошибка при обработке архива:</b>\n<code>{str(e)[:300]}</code>",
            parse_mode="HTML"
        )


# ==================== КОМАНДЫ УПРАВЛЕНИЯ ГРУППАМИ ====================

@dp.message(Command("creategroup"))
async def cmd_create_group(message: Message, command: CommandObject):
    """Создать новую группу: /creategroup [название]"""
    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id

    if not args:
        await message.answer(
            "❌ <b>Использование:</b> <code>/creategroup [название]</code>\n\n"
            "Пример: <code>/creategroup CS2 Main</code>",
            parse_mode="HTML"
        )
        return

    group_name = ' '.join(args)

    if len(group_name) > 100:
        await message.answer("❌ Название группы не должно превышать 100 символов")
        return

    async with AsyncSessionLocal() as session:
        # Проверяем, нет ли уже группы с таким именем
        stmt = select(AccountGroup).where(
            AccountGroup.telegram_id == telegram_id,
            AccountGroup.name == group_name
        )
        result = await session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            await message.answer(
                f"❌ Группа с названием <b>{group_name}</b> уже существует",
                parse_mode="HTML"
            )
            return

        # Получаем максимальный sort_order
        stmt = select(func.max(AccountGroup.sort_order)).where(AccountGroup.telegram_id == telegram_id)
        result = await session.execute(stmt)
        max_order = result.scalar() or 0

        group = AccountGroup(
            telegram_id=telegram_id,
            name=group_name,
            sort_order=max_order + 1
        )
        session.add(group)
        await session.commit()

    await message.answer(
        f"✅ Группа <b>{group_name}</b> создана!\n"
        f"Используйте <code>/add_to_group [аккаунт] {group_name}</code> для добавления аккаунтов.",
        parse_mode="HTML"
    )


@dp.message(Command("delgroup"))
@dp.message(Command("deletegroup"))
@dp.message(Command("removegroup"))
async def cmd_delete_group(message: Message, command: CommandObject):
    """Удалить группу: /delgroup [название]"""
    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id

    if not args:
        await message.answer(
            "❌ <b>Использование:</b> <code>/delgroup [название]</code>\n\n"
            "Пример: <code>/delgroup CS2 Main</code>\n\n"
            "⚠️ Аккаунты не будут удалены, они останутся без группы.",
            parse_mode="HTML"
        )
        return

    group_name = ' '.join(args)

    async with AsyncSessionLocal() as session:
        stmt = select(AccountGroup).where(
            AccountGroup.telegram_id == telegram_id,
            AccountGroup.name == group_name
        )
        result = await session.execute(stmt)
        group = result.scalar_one_or_none()

        if not group:
            await message.answer(f"❌ Группа <b>{group_name}</b> не найдена", parse_mode="HTML")
            return

        # Считаем сколько аккаунтов в группе
        stmt = select(func.count(Mafile.id)).where(Mafile.group_id == group.id)
        result = await session.execute(stmt)
        count = result.scalar() or 0

        await session.delete(group)
        await session.commit()

    await message.answer(
        f"✅ Группа <b>{group_name}</b> удалена!\n"
        f"📱 {count} аккаунтов остались без группы.",
        parse_mode="HTML"
    )


@dp.message(Command("add_to_group"))
async def cmd_add_to_group(message: Message, command: CommandObject):
    """Добавить аккаунт в группу: /add_to_group [аккаунт] [группа]"""
    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id

    if len(args) < 2:
        await message.answer(
            "❌ <b>Использование:</b> <code>/add_to_group [аккаунт] [группа]</code>\n\n"
            "Примеры:\n"
            "<code>/add_to_group 1 CS2 Main</code>\n"
            "<code>/add_to_group mylogin CS2 Main</code>",
            parse_mode="HTML"
        )
        return

    account_input = args[0]
    group_name = ' '.join(args[1:])

    async with AsyncSessionLocal() as session:
        # Ищем аккаунт
        stmt = select(Mafile).where(Mafile.telegram_id == telegram_id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        mafile = None
        if account_input.isdigit():
            idx = int(account_input) - 1
            if 0 <= idx < len(mafiles):
                mafile = mafiles[idx]
        else:
            mafile = next((mf for mf in mafiles if mf.account_name.lower() == account_input.lower()), None)

        if not mafile:
            await message.answer(f"❌ Аккаунт <b>{account_input}</b> не найден", parse_mode="HTML")
            return

        # Ищем группу
        stmt = select(AccountGroup).where(
            AccountGroup.telegram_id == telegram_id,
            AccountGroup.name == group_name
        )
        result = await session.execute(stmt)
        group = result.scalar_one_or_none()

        if not group:
            await message.answer(
                f"❌ Группа <b>{group_name}</b> не найдена\n\n"
                f"Создайте группу: <code>/creategroup {group_name}</code>",
                parse_mode="HTML"
            )
            return

        old_group_name = mafile.group.name if mafile.group else "Без группы"
        mafile.group_id = group.id
        await session.commit()

    await message.answer(
        f"✅ Аккаунт <b>{mafile.account_name}</b> перемещён:\n"
        f"{old_group_name} → <b>{group_name}</b>",
        parse_mode="HTML"
    )


@dp.message(Command("remove_from_group"))
@dp.message(Command("ungroup"))
async def cmd_remove_from_group(message: Message, command: CommandObject):
    """Убрать аккаунт из группы: /remove_from_group [аккаунт]"""
    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id

    if not args:
        await message.answer(
            "❌ <b>Использование:</b> <code>/remove_from_group [аккаунт]</code>\n\n"
            "Примеры:\n"
            "<code>/remove_from_group 1</code>\n"
            "<code>/remove_from_group mylogin</code>",
            parse_mode="HTML"
        )
        return

    account_input = args[0]

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.telegram_id == telegram_id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        mafile = None
        if account_input.isdigit():
            idx = int(account_input) - 1
            if 0 <= idx < len(mafiles):
                mafile = mafiles[idx]
        else:
            mafile = next((mf for mf in mafiles if mf.account_name.lower() == account_input.lower()), None)

        if not mafile:
            await message.answer(f"❌ Аккаунт <b>{account_input}</b> не найден", parse_mode="HTML")
            return

        if not mafile.group_id:
            await message.answer(
                f"❌ Аккаунт <b>{mafile.account_name}</b> уже без группы",
                parse_mode="HTML"
            )
            return

        old_group_name = mafile.group.name if mafile.group else "Без группы"
        mafile.group_id = None
        await session.commit()

    await message.answer(
        f"✅ Аккаунт <b>{mafile.account_name}</b> убран из группы <b>{old_group_name}</b>",
        parse_mode="HTML"
    )


@dp.message(Command("showgroup"))
@dp.message(Command("group"))
async def cmd_show_group(message: Message, command: CommandObject):
    """Показать аккаунты в группе: /showgroup [название]"""
    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id

    if not args:
        # Показываем все группы
        await cmd_groups(message)
        return

    group_name = ' '.join(args)

    async with AsyncSessionLocal() as session:
        stmt = select(AccountGroup).where(
            AccountGroup.telegram_id == telegram_id,
            AccountGroup.name == group_name
        )
        result = await session.execute(stmt)
        group = result.scalar_one_or_none()

        if not group:
            await message.answer(f"❌ Группа <b>{group_name}</b> не найдена", parse_mode="HTML")
            return

        # Получаем аккаунты группы
        stmt = select(Mafile).where(Mafile.group_id == group.id).order_by(
            Mafile.is_pinned.desc(),
            Mafile.sort_order.asc(),
            Mafile.id.asc()
        )
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await message.answer(
                f"📁 <b>{group_name}</b>\n\n❌ В этой группе нет аккаунтов",
                parse_mode="HTML"
            )
            return

        # Формируем список
        text = f"📁 <b>{group_name}</b> ({len(mafiles)} акк.)\n\n"

        for idx, mf in enumerate(mafiles, 1):
            pin = "📌" if mf.is_pinned else ""
            guard = "🔐" if mf.shared_secret else "❌"
            has_login = "✅" if (mf.access_token and mf.refresh_token and mf.password and mf.password.strip()) else "⚠️"

            if mf.steamid:
                steamid_str = str(mf.steamid)
                steamid_short = f"[{steamid_str[:4]}...{steamid_str[-4:]}]" if len(steamid_str) > 8 else f"[{steamid_str}]"
            else:
                steamid_short = "[нет]"

            text += f"{idx}. {pin}{has_login}{guard} <code>{mf.account_name}</code> {steamid_short}\n"

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="📋 Открыть в меню",
                    callback_data=f"list_group_id_{group.id}_0"
                )],
                [InlineKeyboardButton(
                    text="📂 Все группы",
                    callback_data="groups_menu"
                )]
            ]
        )

        await message.answer(text, parse_mode="HTML", reply_markup=keyboard)

@dp.message(Command("groups"))
async def cmd_groups(message: Message):
    """Просмотр групп"""
    telegram_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(AccountGroup).where(AccountGroup.telegram_id == telegram_id).order_by(AccountGroup.sort_order)
        result = await session.execute(stmt)
        groups = result.scalars().all()

        stmt = select(func.count(Mafile.id)).where(Mafile.telegram_id == telegram_id, Mafile.group_id == None)
        result = await session.execute(stmt)
        no_group_count = result.scalar() or 0

    keyboard_buttons = []

    if no_group_count > 0:
        keyboard_buttons.append([
            InlineKeyboardButton(
                text=f"📂 Без группы ({no_group_count})",
                callback_data="list_group_none_0"
            )
        ])

    for grp in groups:
        stmt = select(func.count(Mafile.id)).where(Mafile.group_id == grp.id)
        result = await session.execute(stmt)
        count = result.scalar() or 0

        keyboard_buttons.append([
            InlineKeyboardButton(
                text=f"📁 {grp.name} ({count})",
                callback_data=f"view_group_{grp.id}_0"
            )
        ])

    keyboard_buttons.append([
        InlineKeyboardButton(text="➕ Создать группу", callback_data="create_group")
    ])
    keyboard_buttons.append([
        InlineKeyboardButton(text="📋 Все аккаунты", callback_data="list_group_all_0")
    ])
    keyboard_buttons.append([
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_start")
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    await message.answer(
        "📂 <b>ГРУППЫ АККАУНТОВ</b>\n\n"
        f"Всего групп: {len(groups)}\n"
        f"Аккаунтов без группы: {no_group_count}\n\n"
        "Выберите группу для просмотра:",
        parse_mode="HTML",
        reply_markup=keyboard
    )

@dp.message(Command("exportgroup"))
@dp.message(Command("export_group"))
async def cmd_export_group(message: Message, command: CommandObject):
    """Экспорт всех аккаунтов из группы в ZIP: /exportgroup [название]"""
    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id

    if not args:
        await message.answer(
            "❌ <b>Использование:</b> <code>/exportgroup [название]</code>\n\n"
            "Пример: <code>/exportgroup CS2 Main</code>",
            parse_mode="HTML"
        )
        return

    group_name = ' '.join(args)

    async with AsyncSessionLocal() as session:
        # Ищем группу
        stmt = select(AccountGroup).where(
            AccountGroup.telegram_id == telegram_id,
            AccountGroup.name == group_name
        )
        result = await session.execute(stmt)
        group = result.scalar_one_or_none()

        if not group:
            await message.answer(f"❌ Группа <b>{group_name}</b> не найдена", parse_mode="HTML")
            return

        # Получаем аккаунты группы
        stmt = select(Mafile).where(Mafile.group_id == group.id).order_by(
            Mafile.is_pinned.desc(),
            Mafile.sort_order.asc(),
            Mafile.id.asc()
        )
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await message.answer(
                f"📁 <b>{group_name}</b>\n\n❌ В этой группе нет аккаунтов для экспорта",
                parse_mode="HTML"
            )
            return

        status_msg = await message.answer(
            f"🔄 <b>Создаю архив группы \"{group_name}\"...</b>",
            parse_mode="HTML"
        )

        try:
            # Создаём ZIP архив в памяти
            zip_buffer = io.BytesIO()
            exported_count = 0

            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for mafile in mafiles:
                    mafile_data = build_mafile_dict(mafile)

                    if mafile_data:
                        json_str = json.dumps(mafile_data, indent=2, ensure_ascii=False)
                        filename = f"{mafile.account_name}.mafile"
                        zip_file.writestr(filename, json_str)
                        exported_count += 1

            zip_buffer.seek(0)


            # Формируем список аккаунтов
            accounts_list = "\n".join([f"• <code>{mf.account_name}</code>" for mf in mafiles[:20]])
            if len(mafiles) > 20:
                accounts_list += f"\n<i>... и еще {len(mafiles) - 20}</i>"

            await status_msg.edit_text(
                f"📤 <b>Экспорт группы \"{group_name}\"</b>\n\n"
                f"📱 Аккаунтов: {exported_count}\n"
                f"📦 Формат: ZIP архив\n\n"
                f"<b>Аккаунты в архиве:</b>\n{accounts_list}",
                parse_mode="HTML"
            )

            # Безопасное имя файла (убираем спецсимволы)
            safe_name = re.sub(r'[^\w\s-]', '', group_name).strip()[:50]
            if not safe_name:
                safe_name = "group"

            await message.answer_document(
                BufferedInputFile(
                    zip_buffer.getvalue(),
                    filename=f"{safe_name}_mafiles.zip"
                ),
                caption=f"📦 Группа \"{group_name}\": {exported_count} аккаунтов"
            )

        except Exception as e:
            await status_msg.edit_text(
                f"❌ Ошибка при создании архива: {str(e)[:200]}",
                parse_mode="HTML"
            )


async def handle_mafile_for_account(message: Message, state_data: dict, user_id: int):
    """Обработка mafile файла для аккаунта"""
    mafile_id = state_data.get("mafile_id")
    account_name = state_data.get("account_name")
    client = state_data.get("client")
    db_session = state_data.get("db_session")
    document = message.document

    if not document:
        return

    if not document.file_name.lower().endswith('.mafile') and not document.file_name.lower().endswith('.json'):
        await message.answer(
            "⚠️ Файл должен иметь расширение .mafile или .json\n"
            "Попробуйте снова с правильным файлом."
        )
        return

    status_msg = await message.answer("🔄 Обрабатываю файл...")

    try:
        # Скачиваем файл
        file_id = document.file_id
        file = await bot.get_file(file_id)
        file_path = file.file_path

        import io
        file_bytes = io.BytesIO()
        await bot.download_file(file_path, file_bytes)
        file_bytes.seek(0)

        file_content = file_bytes.read().decode('utf-8')
        mafile_data = json.loads(file_content)

        # Проверяем что account_name совпадает
        if mafile_data.get('account_name') != account_name:
            await status_msg.edit_text(
                f"❌ <b>Ошибка!</b>\n\n"
                f"В файле указан аккаунт <code>{mafile_data.get('account_name')}</code>\n"
                f"Но ожидается <code>{account_name}</code>",
                parse_mode="HTML"
            )
            return

        if 'shared_secret' not in mafile_data:
            await status_msg.edit_text("❌ В файле отсутствует shared_secret", parse_mode="HTML")
            return

        async with AsyncSessionLocal() as session:
            mafile = await get_owned_mafile(session, mafile_id, message.from_user.id)

            if not mafile:
                await status_msg.edit_text("❌ Аккаунт не найден в базе")
                return

            # Сохраняем данные из mafile (шифруем)
            mafile.shared_secret = encrypt_value(mafile_data.get('shared_secret'))
            mafile.identity_secret = encrypt_value(mafile_data.get('identity_secret'))
            mafile.secret_1 = encrypt_value(mafile_data.get('secret_1'))
            mafile.device_id = encrypt_value(mafile_data.get('device_id'))
            mafile.serial_number = encrypt_value(mafile_data.get('serial_number'))
            mafile.revocation_code = encrypt_value(mafile_data.get('revocation_code'))
            mafile.token_gid = encrypt_value(mafile_data.get('token_gid'))
            mafile.uri = encrypt_value(mafile_data.get('uri'))
            mafile.fully_enrolled = True
            mafile.updated_at = datetime.utcnow()

            # Если есть сессия в mafile - сохраняем
            if 'Session' in mafile_data:
                mafile.access_token = encrypt_value(mafile_data['Session'].get('AccessToken'))
                mafile.refresh_token = encrypt_value(mafile_data['Session'].get('RefreshToken'))
                if mafile_data['Session'].get('SteamID'):
                    mafile.steamid = int(mafile_data['Session']['SteamID'])

            await session.commit()

            # Загружаем ключи в клиент (расшифровываем для использования)
            client.load_mobile({
                'shared_secret': decrypt_value(mafile.shared_secret),
                'device_id': decrypt_value(mafile.device_id) or generate_device_id()
            })

            # Генерируем тестовый код
            await client.align_time()
            code = client.generate_steam_guard_code()

            # Продолжаем вход с кодом
            await status_msg.edit_text(
                f"✅ <b>Steam Guard импортирован!</b>\n\n"
                f"📱 Аккаунт: <code>{account_name}</code>\n"
                f"🔐 Код: <code>{code}</code>\n\n"
                f"Отправьте этот код для подтверждения входа.",
                parse_mode="HTML"
            )

            # Сохраняем состояние для ввода кода
            user_states[user_id] = {
                "state": "waiting_steam_code",
                "account_name": account_name,
                "client": client,
                "mafile_id": mafile_id,
                "db_session": db_session
            }

    except json.JSONDecodeError as e:
        await status_msg.edit_text(f"❌ Ошибка JSON: {str(e)}")
        user_states.pop(user_id, None)
        await client.close()
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {str(e)}")
        logger.error(f"Error in handle_mafile_for_account: {e}")
        user_states.pop(user_id, None)
        await client.close()

async def process_steam_code(message: Message, state_data: dict, user_id: int):
    """Обработка кода подтверждения"""
    code = message.text.strip()
    account_name = state_data.get("account_name")
    client = state_data.get("client")
    mafile_id = state_data.get("mafile_id")
    db_session = state_data.get("db_session")

    if not client:
        await message.answer("❌ Сессия истекла. Попробуйте снова /login")
        user_states.pop(user_id, None)
        return

    try:
        try:
            await message.delete()
        except TelegramAPIError:
            pass
        status_msg = await message.answer("🔄 Подтверждаю вход...", parse_mode="HTML")

        await client.confirm_login(code)
        await SteamSessionManager.save_steam_session_to_db(db_session, mafile_id, client)

        # Проверяем, какой тип входа был
        current_state = state_data.get("state")

        if current_state == "waiting_email_code":
            # Вход был с email-кодом - предлагаем подключить Steam Guard
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="🔐 Подключить Steam Guard",
                        callback_data=f"setup_steamguard_{mafile_id}"
                    )],
                    [InlineKeyboardButton(
                        text="👤 К аккаунту",
                        callback_data=f"account_detail_{mafile_id}"
                    )]
                ]
            )

            await status_msg.edit_text(
                f"✅ <b>Успешный вход!</b>\n"
                f"SteamID: <code>{client.steamid}</code>\n\n"
                f"⚠️ <b>Рекомендуется подключить мобильный Steam Guard</b>\n"
                f"Это повысит безопасность и позволит боту генерировать коды.",
                parse_mode="HTML",
                reply_markup=keyboard
            )
        else:
            # Обычный вход с мобильным Guard
            if client.shared_secret:
                await status_msg.edit_text(
                    f"✅ <b>Успешный вход!</b>\n"
                    f"SteamID: <code>{client.steamid}</code>\n\n"
                    f"🔐 Steam Guard работает!\n"
                    f"Используйте <code>/gc {account_name}</code> для получения кодов.",
                    parse_mode="HTML"
                )
            else:
                await status_msg.edit_text(
                    f"✅ <b>Успешный вход!</b>\n"
                    f"SteamID: <code>{client.steamid}</code>",
                    parse_mode="HTML"
                )

    except InvalidSteamGuardCode:
        await message.answer("❌ Неверный код подтверждения. Попробуйте еще раз.")
        return
    except Exception as e:
        await message.answer(f"❌ Ошибка при подтверждении: {str(e)}")
        logger.error(f"Error confirming login for {account_name}: {e}")
    finally:
        await client.close()
        user_states.pop(user_id, None)

async def process_mafile_for_account(message: Message, user_id: int, state_data: dict):
    """Обработка mafile для конкретного аккаунта (из кнопки импорта)"""
    mafile_id = state_data.get("mafile_id")
    account_name = state_data.get("account_name")
    document = message.document

    status_msg = await message.answer("🔄 Обрабатываю файл...")

    try:
        # Скачиваем файл
        file_id = document.file_id
        file = await bot.get_file(file_id)
        file_path = file.file_path

        import io
        file_bytes = io.BytesIO()
        await bot.download_file(file_path, file_bytes)
        file_bytes.seek(0)

        file_content = file_bytes.read().decode('utf-8')
        mafile_data = json.loads(file_content)

        # Проверяем что account_name совпадает
        if mafile_data.get('account_name') != account_name:
            await status_msg.edit_text(
                f"❌ <b>Ошибка!</b>\n\n"
                f"В файле указан аккаунт <code>{mafile_data.get('account_name')}</code>\n"
                f"Но ожидается <code>{account_name}</code>\n\n"
                f"Отправьте правильный mafile для этого аккаунта.",
                parse_mode="HTML"
            )
            user_states.pop(user_id, None)
            return

        # Проверяем обязательные поля
        if 'shared_secret' not in mafile_data:
            await status_msg.edit_text(
                "❌ В файле отсутствует обязательное поле shared_secret",
                parse_mode="HTML"
            )
            user_states.pop(user_id, None)
            return

        async with AsyncSessionLocal() as session:
            mafile = await get_owned_mafile(session, mafile_id, message.from_user.id)

            if not mafile:
                await status_msg.edit_text("❌ Аккаунт не найден в базе")
                user_states.pop(user_id, None)
                return

            # Обновляем данные (шифруем)
            mafile.shared_secret = encrypt_value(mafile_data.get('shared_secret'))
            mafile.identity_secret = encrypt_value(mafile_data.get('identity_secret'))
            mafile.secret_1 = encrypt_value(mafile_data.get('secret_1'))
            mafile.device_id = encrypt_value(mafile_data.get('device_id'))
            mafile.serial_number = encrypt_value(mafile_data.get('serial_number'))
            mafile.revocation_code = encrypt_value(mafile_data.get('revocation_code'))
            mafile.token_gid = encrypt_value(mafile_data.get('token_gid'))
            mafile.uri = encrypt_value(mafile_data.get('uri'))
            mafile.fully_enrolled = True
            mafile.updated_at = datetime.utcnow()

            # Если есть сессия - сохраняем (шифруем)
            if 'Session' in mafile_data:
                mafile.access_token = encrypt_value(mafile_data['Session'].get('AccessToken'))
                mafile.refresh_token = encrypt_value(mafile_data['Session'].get('RefreshToken'))
                if mafile_data['Session'].get('SteamID'):
                    mafile.steamid = int(mafile_data['Session']['SteamID'])

            await session.commit()

            # Проверяем генерацию кода
            password = decrypt_value(mafile.password) if mafile.password else ""
            client = AsyncSteamMobile(mafile.account_name, password)
            client.load_mobile({
                'shared_secret': decrypt_value(mafile.shared_secret),
                'device_id': decrypt_value(mafile.device_id) or generate_device_id()
            })

            try:
                await client.align_time()
                code = client.generate_steam_guard_code()

                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="🔐 Получить код",
                            callback_data=f"action_get_code_{mafile.id}"
                        )],
                        [InlineKeyboardButton(
                            text="👤 К аккаунту",
                            callback_data=f"account_detail_{mafile.id}"
                        )]
                    ]
                )

                await status_msg.edit_text(
                    f"✅ <b>Steam Guard успешно импортирован!</b>\n\n"
                    f"📱 <b>Аккаунт:</b> <code>{account_name}</code>\n"
                    f"🔐 <b>Тестовый код:</b> <code>{code}</code>\n\n"
                    f"Теперь вы можете получать коды!",
                    parse_mode="HTML",
                    reply_markup=keyboard
                )

            except Exception as e:
                await status_msg.edit_text(
                    f"⚠️ <b>Данные сохранены, но есть проблема с генерацией кода:</b>\n"
                    f"<code>{str(e)}</code>\n\n"
                    f"Попробуйте выполнить вход для синхронизации.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="🔑 Войти",
                                callback_data=f"action_login_{mafile.id}"
                            )]
                        ]
                    )
                )
            finally:
                await client.close()

    except json.JSONDecodeError as e:
        await status_msg.edit_text(f"❌ Файл поврежден: {str(e)}")
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка при импорте: {str(e)}")
        logger.error(f"Error importing mafile for account {account_name}: {e}")
    finally:
        user_states.pop(user_id, None)

async def process_mafile_during_login(message: Message, user_id: int, state_data: dict):
    """Обработка mafile во время входа"""
    mafile_id = state_data.get("mafile_id")
    account_name = state_data.get("account_name")
    client = state_data.get("client")
    db_session = state_data.get("db_session")
    document = message.document

    status_msg = await message.answer("🔄 Обрабатываю файл...")

    try:
        # Скачиваем и парсим файл
        file_id = document.file_id
        file = await bot.get_file(file_id)
        file_path = file.file_path

        import io
        file_bytes = io.BytesIO()
        await bot.download_file(file_path, file_bytes)
        file_bytes.seek(0)

        file_content = file_bytes.read().decode('utf-8')
        mafile_data = json.loads(file_content)

        if mafile_data.get('account_name') != account_name:
            await status_msg.edit_text(
                f"❌ В файле указан аккаунт <code>{mafile_data.get('account_name')}</code>, "
                f"но ожидается <code>{account_name}</code>",
                parse_mode="HTML"
            )
            return

        if 'shared_secret' not in mafile_data:
            await status_msg.edit_text("❌ В файле отсутствует shared_secret", parse_mode="HTML")
            return

        # Сохраняем в БД (шифруем)
        async with AsyncSessionLocal() as session:
            mafile = await get_owned_mafile(session, mafile_id, message.from_user.id)

            if mafile:
                mafile.shared_secret = encrypt_value(mafile_data.get('shared_secret'))
                mafile.identity_secret = encrypt_value(mafile_data.get('identity_secret'))
                mafile.secret_1 = encrypt_value(mafile_data.get('secret_1'))
                mafile.device_id = encrypt_value(mafile_data.get('device_id'))
                mafile.fully_enrolled = True

                # 🔥 Сохраняем токены из mafile если есть
                if 'Session' in mafile_data:
                    mafile.access_token = encrypt_value(mafile_data['Session'].get('AccessToken'))
                    mafile.refresh_token = encrypt_value(mafile_data['Session'].get('RefreshToken'))
                    if mafile_data['Session'].get('SteamID'):
                        mafile.steamid = int(mafile_data['Session']['SteamID'])

                await session.commit()

        # Загружаем в клиент и генерируем код
        client.load_mobile({
            'shared_secret': mafile_data['shared_secret'],  # из файла, не из БД
            'device_id': mafile_data.get('device_id', generate_device_id())
        })
        await client.align_time()
        code = client.generate_steam_guard_code()

        # Обновляем состояние на ожидание кода
        user_states[user_id] = {
            "state": "waiting_steam_code",
            "account_name": account_name,
            "client": client,
            "mafile_id": mafile_id,
            "db_session": db_session
        }

        await status_msg.edit_text(
            f"✅ <b>Steam Guard импортирован!</b>\n\n"
            f"📱 Аккаунт: <code>{account_name}</code>\n"
            f"🔐 Ваш текущий код: <b><code>{code}</code></b>\n\n"
            f"Отправьте этот код в чат для подтверждения входа.",
            parse_mode="HTML"
        )

    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка: {str(e)}")
        logger.error(f"Error in process_mafile_during_login: {e}")
        user_states.pop(user_id, None)

async def process_bulk_import(message: Message, user_id: int):
    """Обработка массового импорта аккаунтов"""
    text = message.text.strip()

    if not text:
        await message.answer("❌ Отправьте список аккаунтов")
        return

    # Определяем разделитель
    if ';' in text:
        # Формат: login:pass;login2:pass2
        accounts = [acc.strip() for acc in text.split(';') if acc.strip()]
    else:
        # Формат: каждый с новой строки
        accounts = [acc.strip() for acc in text.split('\n') if acc.strip()]

    if not accounts:
        await message.answer("❌ Не найдено ни одного аккаунта в сообщении")
        user_states.pop(user_id, None)
        return

    # Проверяем формат каждого аккаунта
    valid_accounts = []
    invalid_lines = []

    for idx, acc in enumerate(accounts, 1):
        if ':' in acc:
            parts = acc.split(':', 1)
            if len(parts) == 2 and parts[0] and parts[1]:
                valid_accounts.append((parts[0], parts[1]))
            else:
                invalid_lines.append(f"Строка {idx}: {acc[:30]}...")
        else:
            invalid_lines.append(f"Строка {idx}: {acc[:30]}... (нет двоеточия)")

    if not valid_accounts:
        await message.answer(
            f"❌ Ни один аккаунт не прошел проверку формата.\n"
            f"Ожидается: <code>login:password</code>",
            parse_mode="HTML"
        )
        user_states.pop(user_id, None)
        return

    # Отправляем сообщение о начале импорта
    status_msg = await message.answer(
        f"🔄 <b>Начинаю импорт {len(valid_accounts)} аккаунтов...</b>\n"
        f"Пожалуйста, подождите...",
        parse_mode="HTML"
    )

    added = []
    skipped = []
    errors = []

    async with AsyncSessionLocal() as session:
        # Получаем максимальный sort_order
        stmt = select(func.max(Mafile.sort_order)).where(Mafile.telegram_id == user_id)
        result = await session.execute(stmt)
        max_order = result.scalar() or 0

        for idx, (account_name, password) in enumerate(valid_accounts, 1):
            try:
                # Проверяем, существует ли уже такой аккаунт
                stmt = select(Mafile).where(
                    Mafile.telegram_id == user_id,
                    Mafile.account_name == account_name
                )
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()

                if existing:
                    skipped.append(account_name)
                    continue

                # Создаем новый аккаунт
                mafile = Mafile(
                    telegram_id=user_id,
                    account_name=account_name,
                    password=password,
                    unique_proxy=False,
                    sort_order=max_order + idx
                )
                session.add(mafile)
                added.append((account_name, mafile.id))

                # Обновляем статус каждые 5 аккаунтов
                if idx % 5 == 0:
                    await status_msg.edit_text(
                        f"🔄 <b>Импорт аккаунтов...</b>\n"
                        f"Обработано: {idx}/{len(valid_accounts)}\n"
                        f"✅ Добавлено: {len(added)}\n"
                        f"⏭️ Пропущено: {len(skipped)}\n"
                        f"❌ Ошибок: {len(errors)}",
                        parse_mode="HTML"
                    )

            except Exception as e:
                errors.append(f"{account_name}: {str(e)[:50]}")
                logger.error(f"Error adding account {account_name}: {e}")

        # Сохраняем все изменения
        await session.commit()

    # Формируем итоговое сообщение
    result_text = (
        f"📊 <b>РЕЗУЛЬТАТЫ ИМПОРТА</b>\n\n"
        f"✅ <b>Успешно добавлено:</b> {len(added)}\n"
        f"⏭️ <b>Пропущено (уже есть):</b> {len(skipped)}\n"
        f"❌ <b>Ошибок:</b> {len(errors)}\n\n"
    )

    if added:
        result_text += "<b>Добавленные аккаунты:</b>\n"
        for acc_name, _ in added[:10]:
            result_text += f"• <code>{acc_name}</code>\n"
        if len(added) > 10:
            result_text += f"<i>... и еще {len(added) - 10}</i>\n"
        result_text += "\n"

    if skipped:
        result_text += "<b>Пропущенные (уже в базе):</b>\n"
        for acc_name in skipped[:5]:
            result_text += f"• <code>{acc_name}</code>\n"
        if len(skipped) > 5:
            result_text += f"<i>... и еще {len(skipped) - 5}</i>\n"
        result_text += "\n"

    if errors:
        result_text += "<b>Ошибки:</b>\n"
        for err in errors[:5]:
            result_text += f"• {err}\n"
        if len(errors) > 5:
            result_text += f"<i>... и еще {len(errors) - 5}</i>\n"
        result_text += "\n"

    # Добавляем кнопки для дальнейших действий
    keyboard_buttons = []

    if added:
        # Кнопка для просмотра добавленных аккаунтов
        keyboard_buttons.append([
            InlineKeyboardButton(
                text="👤 Посмотреть все аккаунты",
                callback_data="my_accounts"
            )
        ])

    keyboard_buttons.append([
        InlineKeyboardButton(
            text="🏠 Главное меню",
            callback_data="back_to_start"
        )
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    await status_msg.edit_text(result_text, parse_mode="HTML", reply_markup=keyboard)
    user_states.pop(user_id, None)
