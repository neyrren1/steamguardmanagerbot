"""Extracted from the legacy bot module without behavior changes."""

from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from datetime import datetime
from guardbot.bot.runtime import bot, dp, user_states
from guardbot.config import MAX_FILE_SIZE, logger
from guardbot.database import AsyncSessionLocal, Mafile, User
from guardbot.security import decrypt_value, encrypt_value
from guardbot.steam.client import parse_proxy_string, redact_proxy_url
from sqlalchemy import func, select, text
import io
import json

# ==================== ИМПОРТ СУЩЕСТВУЮЩИХ MAFILES ====================
# ДОБАВИТЬ В ФАЙЛ ПОСЛЕ ВСЕХ КОМАНД

@dp.message(Command("import_mafile"))
async def cmd_import_mafile(message: Message):
    """
    Импорт существующего .mafile файла
    Отправьте файл после этой команды
    """
    user_states[message.from_user.id] = {"state": "waiting_mafile"}

    await message.answer(
        "📤 <b>ИМПОРТ MAFILE</b>\n\n"
        "Отправьте мне файл <b>.mafile</b> с данными Steam Guard.\n\n"
        "<b>Где взять .mafile?</b>\n"
        "• Из Steam Desktop Authenticator (SDA)\n"
        "• Из мобильного Steam (нужно извлечь)\n"
        "• Из других программ для Steam Guard\n\n"
        "<b>Формат файла должен быть JSON с полями:</b>\n"
        "• <code>account_name</code> - логин\n"
        "• <code>shared_secret</code> - секрет для кодов\n"
        "• <code>identity_secret</code> - секрет для подтверждений\n"
        "• <code>device_id</code> - ID устройства\n"
        "• <code>serial_number</code> - серийный номер\n"
        "• <code>revocation_code</code> - код восстановления\n\n"
        "Отправьте файл прямо в чат.",
        parse_mode="HTML"
    )

async def process_mafile_import(message: Message, user_id: int, state_data: dict):
    """Обработка mafile через команду /import_mafile"""
    document = message.document
    status_msg = await message.answer("🔄 Обрабатываю файл...")

    try:
        await message.delete()
    except:
        pass

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

        account_name_from_file = mafile_data.get('account_name')
        if not account_name_from_file:
            await status_msg.edit_text("❌ В файле не указан account_name")
            user_states.pop(user_id, None)
            return

        if 'shared_secret' not in mafile_data:
            await status_msg.edit_text("❌ В файле отсутствует shared_secret")
            user_states.pop(user_id, None)
            return

        async with AsyncSessionLocal() as session:
            stmt = select(Mafile).where(
                Mafile.telegram_id == user_id,
                Mafile.account_name == account_name_from_file
            )
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

            if existing:
                # Обновляем существующий (шифруем все чувствительные поля)
                existing.shared_secret = encrypt_value(mafile_data.get('shared_secret'))
                existing.identity_secret = encrypt_value(mafile_data.get('identity_secret'))
                existing.secret_1 = encrypt_value(mafile_data.get('secret_1'))
                existing.device_id = encrypt_value(mafile_data.get('device_id'))
                existing.serial_number = encrypt_value(mafile_data.get('serial_number'))
                existing.revocation_code = encrypt_value(mafile_data.get('revocation_code'))
                existing.token_gid = encrypt_value(mafile_data.get('token_gid'))
                existing.uri = encrypt_value(mafile_data.get('uri'))
                existing.fully_enrolled = True

                if 'Session' in mafile_data:
                    existing.access_token = encrypt_value(mafile_data['Session'].get('AccessToken'))
                    existing.refresh_token = encrypt_value(mafile_data['Session'].get('RefreshToken'))

                await session.commit()

                await status_msg.edit_text(
                    f"✅ Аккаунт <b>{account_name_from_file}</b> обновлен!\n"
                    f"Steam Guard данные успешно импортированы.",
                    parse_mode="HTML"
                )
            else:
                # Создаем новый - нужен пароль
                user_states[user_id] = {
                    "state": "waiting_password_for_mafile",
                    "mafile_data": mafile_data
                }

                await status_msg.edit_text(
                    f"📝 Аккаунт <b>{account_name_from_file}</b>\n\n"
                    f"Для завершения импорта введите <b>ПАРОЛЬ</b> от этого аккаунта.",
                    parse_mode="HTML"
                )

    except json.JSONDecodeError as e:
        await status_msg.edit_text(f"❌ Файл поврежден: {str(e)}")
        user_states.pop(user_id, None)
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка при импорте: {str(e)}")
        logger.error(f"Error importing mafile: {e}")
        user_states.pop(user_id, None)

async def process_mafile_auto(message: Message, user_id: int):
    """Автоматический импорт mafile без состояния"""
    document = message.document
    status_msg = await message.answer("🔄 Обнаружен mafile файл, обрабатываю...")

    try:
        file_id = document.file_id
        file = await bot.get_file(file_id)
        file_path = file.file_path

        import io
        file_bytes = io.BytesIO()
        await bot.download_file(file_path, file_bytes)
        file_bytes.seek(0)

        file_content = file_bytes.read().decode('utf-8')
        mafile_data = json.loads(file_content)

        account_name = mafile_data.get('account_name')
        if not account_name:
            await status_msg.edit_text("❌ В файле не указан account_name")
            return

        async with AsyncSessionLocal() as session:
            stmt = select(Mafile).where(
                Mafile.telegram_id == user_id,
                Mafile.account_name == account_name
            )
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

            if existing:
                existing.shared_secret = encrypt_value(mafile_data.get('shared_secret'))
                existing.identity_secret = encrypt_value(mafile_data.get('identity_secret'))
                existing.device_id = encrypt_value(mafile_data.get('device_id'))
                existing.fully_enrolled = True

                if 'Session' in mafile_data:
                    existing.access_token = encrypt_value(mafile_data['Session'].get('AccessToken'))
                    existing.refresh_token = encrypt_value(mafile_data['Session'].get('RefreshToken'))
                    if mafile_data['Session'].get('SteamID'):
                        existing.steamid = int(mafile_data['Session']['SteamID'])

                await session.commit()

                await status_msg.edit_text(
                    f"✅ Аккаунт <b>{account_name}</b> обновлен!\n"
                    f"Steam Guard данные импортированы.\n\n"
                    f"Используйте <code>/gc {account_name}</code> для получения кода.",
                    parse_mode="HTML"
                )
            else:
                user_states[user_id] = {
                    "state": "waiting_password_for_mafile",
                    "mafile_data": mafile_data
                }

                await status_msg.edit_text(
                    f"📝 Аккаунт <b>{account_name}</b> не найден в базе.\n\n"
                    f"Для завершения импорта введите <b>ПАРОЛЬ</b> от этого аккаунта.",
                    parse_mode="HTML"
                )

    except json.JSONDecodeError as e:
        await status_msg.edit_text(f"❌ Файл поврежден: {str(e)}")
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка при импорте: {str(e)}")
        logger.error(f"Error importing mafile: {e}")

async def process_single_mafile(message: Message, user_id: int):
    """Обработка одного mafile файла"""
    document = message.document
    file_name = document.file_name

    # 🔥 Защита: эскейпим имя файла для HTML
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

    # 🔥 Удаляем сообщение с файлом
    try:
        await message.delete()
    except:
        pass

    status_msg = await message.answer(f"🔄 Обрабатываю {safe_file_name}...")

    try:
        # Скачиваем файл
        file_id = document.file_id
        file = await bot.get_file(file_id)
        file_path = file.file_path

        import io
        file_bytes = io.BytesIO()
        await bot.download_file(file_path, file_bytes)
        file_bytes.seek(0)

        # Читаем JSON
        try:
            file_content = file_bytes.read().decode('utf-8')
            mafile_data = json.loads(file_content)
        except UnicodeDecodeError:
            file_bytes.seek(0)
            file_content = file_bytes.read().decode('cp1251')
            mafile_data = json.loads(file_content)

        account_name = mafile_data.get('account_name')
        shared_secret = mafile_data.get('shared_secret')

        if not account_name:
            await status_msg.edit_text(f"❌ В файле {safe_file_name} не указан account_name")
            return

        if not shared_secret:
            await status_msg.edit_text(f"❌ В файле {safe_file_name} отсутствует shared_secret")
            return

        async with AsyncSessionLocal() as session:
            stmt = select(Mafile).where(
                Mafile.telegram_id == user_id,
                Mafile.account_name == account_name
            )
            result = await session.execute(stmt)
            existing = result.scalar_one_or_none()

            if existing:
                # Обновляем существующий (шифруем все чувствительные поля)
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

                await status_msg.edit_text(
                    f"✅ <b>{account_name}</b> обновлен!\n"
                    f"Steam Guard импортирован.",
                    parse_mode="HTML"
                )
            else:
                # Создаем новый аккаунт (шифруем все чувствительные поля)
                steamid = None
                if 'Session' in mafile_data and mafile_data['Session'].get('SteamID'):
                    steamid = int(mafile_data['Session']['SteamID'])
                elif 'steamid' in mafile_data:
                    try:
                        steamid = int(mafile_data['steamid'])
                    except:
                        pass

                # Получаем максимальный sort_order
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

                await status_msg.edit_text(
                    f"✅ <b>{account_name}</b> создан!\n"
                    f"⚠️ Пароль не указан. Добавьте пароль в деталях аккаунта для входа.",
                    parse_mode="HTML"
                )

    except json.JSONDecodeError as e:
        await status_msg.edit_text(f"❌ {safe_file_name}: ошибка JSON")
    except Exception as e:
        await status_msg.edit_text(f"❌ {safe_file_name}: {str(e)[:100]}")
        logger.error(f"Error processing mafile {file_name}: {e}")

# ==================== НОВЫЕ КОМАНДЫ ДЛЯ УПРАВЛЕНИЯ ПРОКСИ ====================
# ВСТАВИТЬ: Эти команды добавить перед функцией main()

@dp.message(Command("set_proxy"))
async def cmd_set_proxy(message: Message, command: CommandObject):
    """
    Установка общего прокси для всех аккаунтов пользователя
    """
    args = command.args.split() if command.args else []

    if not args:
        await message.answer(
            "❌ <b>Использование:</b> <code>/set_proxy [proxy_url]</code>\n\n"
            "<b>Поддерживаемые протоколы:</b>\n"
            "• <code>http://</code> - HTTP прокси\n"
            "• <code>https://</code> - HTTPS прокси\n"
            "• <code>socks5://</code> - SOCKS5 прокси\n"
            "• <code>socks4://</code> - SOCKS4 прокси\n\n"
            "<b>Примеры:</b>\n"
            "• HTTP: <code>/set_proxy http://user:pass@proxy.example.com:8080</code>\n"
            "• SOCKS5: <code>/set_proxy socks5://user:pass@proxy.example.com:1080</code>\n"
            "• SOCKS5 без авторизации: <code>/set_proxy socks5://proxy.example.com:1080</code>",
            parse_mode="HTML"
        )
        return

    try:
        proxy_url = parse_proxy_string(args[0])
    except ValueError:
        await message.answer(
            "❌ <b>Некорректный или небезопасный адрес прокси.</b>\n\n"
            "Используйте публичный хост и формат "
            "<code>protocol://user:password@host:port</code>.",
            parse_mode="HTML"
        )
        return

    try:
        await message.delete()
    except TelegramAPIError:
        pass

    # Определяем тип прокси для информативности
    proxy_type = "HTTP"
    if proxy_url.startswith('socks5://'):
        proxy_type = "SOCKS5"
    elif proxy_url.startswith('socks4://'):
        proxy_type = "SOCKS4"
    elif proxy_url.startswith('https://'):
        proxy_type = "HTTPS"

    telegram_id = message.from_user.id

    try:
        async with AsyncSessionLocal() as session:
            stmt = select(User).where(User.telegram_id == telegram_id)
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()

            if not user:
                user = User(
                    telegram_id=telegram_id,
                    username=message.from_user.username,
                    full_name=message.from_user.full_name,
                    general_proxy=encrypt_value(proxy_url)
                )
                session.add(user)
                logger.info("Created user %s with %s proxy", telegram_id, proxy_type)
            else:
                user.general_proxy = encrypt_value(proxy_url)
                logger.info("Updated %s proxy for user %s", proxy_type, telegram_id)

            await session.commit()

            await message.answer(
                f"✅ <b>Общий {proxy_type} прокси установлен!</b>\n\n"
                f"🌐 <b>Прокси:</b> <code>{redact_proxy_url(proxy_url)}</code>\n\n"
                f"Этот прокси будет использоваться для всех аккаунтов.",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error("Error setting proxy: %s", type(e).__name__)
        await message.answer("❌ Не удалось сохранить прокси. Проверьте формат адреса.")

@dp.message(Command("set_account_proxy"))
async def cmd_set_account_proxy(message: Message, command: CommandObject):
    """
    Установка уникального прокси для конкретного аккаунта
    Пример: /set_account_proxy mylogin http://user:pass@host:port
    """
    args = command.args.split() if command.args else []

    if len(args) < 2:
        await message.answer(
            "❌ <b>Использование:</b> <code>/set_account_proxy [account] [proxy_url]</code>\n\n"
            "<b>Примеры:</b>\n"
            "• По имени аккаунта:\n<code>/set_account_proxy mylogin http://user:pass@proxy.example.com:8080</code>\n"
            "• По номеру:\n<code>/set_account_proxy 1 socks5://user:pass@proxy.example.com:1080</code>\n\n"
            "<b>Форматы прокси:</b>\n"
            "• <code>protocol://username:password@host:port</code>\n"
            "• <code>protocol://host:port</code> (без авторизации)",
            parse_mode="HTML"
        )
        return

    account_input = args[0]
    try:
        proxy_url = parse_proxy_string(args[1])
    except ValueError:
        await message.answer("❌ Некорректный или небезопасный адрес прокси.")
        return

    try:
        await message.delete()
    except TelegramAPIError:
        pass
    telegram_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        # Получаем список аккаунтов
        stmt = select(Mafile).where(Mafile.telegram_id == telegram_id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await message.answer("❌ У вас нет сохраненных аккаунтов")
            return

        mafile = None

        # Ищем аккаунт по номеру или имени
        if account_input.isdigit():
            idx = int(account_input) - 1
            if 0 <= idx < len(mafiles):
                mafile = mafiles[idx]
        else:
            mafile = next((mf for mf in mafiles if mf.account_name == account_input), None)

        if not mafile:
            await message.answer(f"❌ Аккаунт <b>{account_input}</b> не найден", parse_mode="HTML")
            return

        # Устанавливаем уникальный прокси
        mafile.unique_proxy = True
        mafile.proxy = encrypt_value(proxy_url)
        await session.commit()

        await message.answer(
            f"✅ <b>Уникальный прокси установлен!</b>\n\n"
            f"📱 <b>Аккаунт:</b> <code>{mafile.account_name}</code>\n"
            f"🌐 <b>Прокси:</b> <code>{redact_proxy_url(proxy_url)}</code>\n\n"
            f"Этот прокси будет использоваться только для этого аккаунта.",
            parse_mode="HTML"
        )

@dp.message(Command("remove_account_proxy"))
async def cmd_remove_account_proxy(message: Message, command: CommandObject):
    """
    Удаление уникального прокси с аккаунта
    Пример: /remove_account_proxy mylogin
    """
    args = command.args.split() if command.args else []

    if not args:
        await message.answer(
            "❌ <b>Использование:</b> <code>/remove_account_proxy [account]</code>\n\n"
            "<b>Примеры:</b>\n"
            "• <code>/remove_account_proxy mylogin</code>\n"
            "• <code>/remove_account_proxy 1</code>",
            parse_mode="HTML"
        )
        return

    account_input = args[0]
    telegram_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.telegram_id == telegram_id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await message.answer("❌ У вас нет сохраненных аккаунтов")
            return

        mafile = None

        if account_input.isdigit():
            idx = int(account_input) - 1
            if 0 <= idx < len(mafiles):
                mafile = mafiles[idx]
        else:
            mafile = next((mf for mf in mafiles if mf.account_name == account_input), None)

        if not mafile:
            await message.answer(f"❌ Аккаунт <b>{account_input}</b> не найден", parse_mode="HTML")
            return

        # Удаляем уникальный прокси
        mafile.unique_proxy = False
        mafile.proxy = None
        await session.commit()

        await message.answer(
            f"✅ <b>Уникальный прокси удален!</b>\n\n"
            f"📱 <b>Аккаунт:</b> <code>{mafile.account_name}</code>\n\n"
            f"Теперь будет использоваться общий прокси.",
            parse_mode="HTML"
        )

@dp.message(Command("my_proxy"))
async def cmd_my_proxy(message: Message):
    """
    Просмотр текущих настроек прокси
    """
    telegram_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        # Получаем пользователя
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        # Получаем аккаунты
        stmt = select(Mafile).where(Mafile.telegram_id == telegram_id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        text = "🌐 <b>НАСТРОЙКИ ПРОКСИ</b>\n\n"

        # Общий прокси
        text += "<b>📌 Общий прокси:</b>\n"
        if user and user.general_proxy:
            text += f"✅ <code>{redact_proxy_url(decrypt_value(user.general_proxy))}</code>\n"
            text += "💡 <code>/remove_proxy</code> - удалить общий прокси\n\n"
        else:
            text += "❌ <i>Не настроен</i>\n"
            text += "💡 <code>/set_proxy [url]</code> - настроить общий прокси\n\n"

        # Уникальные прокси аккаунтов
        if mafiles:
            text += "<b>📱 Уникальные прокси аккаунтов:</b>\n"
            has_unique = False
            for idx, mf in enumerate(mafiles, 1):
                if mf.unique_proxy and mf.proxy:
                    has_unique = True
                    text += f"{idx}. <code>{mf.account_name}</code>\n"
                    text += f"   🌐 <code>{redact_proxy_url(decrypt_value(mf.proxy))}</code>\n"
                    text += f"   💡 <code>/remove_account_proxy {mf.account_name}</code>\n"

            if not has_unique:
                text += "❌ <i>Нет аккаунтов с уникальными прокси</i>\n"

        # 🔥 Информация о доступных функциях
        has_any_proxy = (user and user.general_proxy) or any(mf.unique_proxy and mf.proxy for mf in mafiles)

        text += "\n━━━━━━━━━━━━━━━━━━━━\n"
        text += "<b>📋 Доступные функции:</b>\n"

        if has_any_proxy:
            text += "✅ Получение Steam Guard кодов\n"
            text += "✅ Вход в аккаунты\n"
            text += "✅ Просмотр и принятие трейдов\n"
            text += "✅ Подтверждения\n"
            text += "✅ Просмотр инвентаря\n"
        else:
            text += "✅ Получение Steam Guard кодов\n"
            text += "❌ Вход в аккаунты (требуется прокси)\n"
            text += "❌ Трейды (требуется прокси)\n"
            text += "❌ Подтверждения (требуется прокси)\n"
            text += "❌ Инвентарь (требуется прокси)\n"

        await message.answer(text, parse_mode="HTML")
