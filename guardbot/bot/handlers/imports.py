"""Extracted from the legacy bot module without behavior changes."""

from collections.abc import Buffer
from html import escape

from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram import F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from datetime import UTC, datetime
from guardbot.bot.filters import NonCommandInputFilter
from guardbot.bot.runtime import (
    bot,
    clear_user_workflows,
    delete_secret_message_or_abort,
    dp,
)
from guardbot.bot.states import NewMaFileImportFlow
from guardbot.config import logger
from guardbot.database import AsyncSessionLocal, Mafile, User
from guardbot.security import decrypt_value, encrypt_value
from guardbot.steam.client import parse_proxy_string, redact_proxy_url
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
import io
import json

_MAX_NEW_MAFILE_BYTES = 1_048_576
_MAX_MAFILE_VALUE_LENGTH = 65_536
_MAX_ACCOUNT_NAME_LENGTH = 255
_NEW_MAFILE_ENCRYPTED_FIELDS = (
    "shared_secret",
    "identity_secret",
    "secret_1",
    "device_id",
    "serial_number",
    "revocation_code",
    "token_gid",
    "uri",
)


# ==================== ИМПОРТ СУЩЕСТВУЮЩИХ MAFILES ====================
# ДОБАВИТЬ В ФАЙЛ ПОСЛЕ ВСЕХ КОМАНД


async def _prepare_secret_command(message: Message) -> bool:
    """Delete secret-bearing commands and reject non-private use."""
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    if message.chat.type == ChatType.PRIVATE:
        return True
    await message.answer(
        "❌ Секретные данные можно вводить только в личном чате с ботом."
    )
    return False

@dp.message(Command("import_mafile"))
async def cmd_import_mafile(message: Message, state: FSMContext):
    """
    Импорт существующего .mafile файла
    Отправьте файл после этой команды
    """
    if not await _prepare_secret_command(message):
        return
    if message.from_user is None:
        raise RuntimeError("Import command has no user")
    user_id = message.from_user.id

    try:
        await clear_user_workflows(user_id, state)
        await state.set_state(NewMaFileImportFlow.waiting_document)
        await state.set_data({})
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
            parse_mode="HTML",
        )
    except BaseException:
        await clear_user_workflows(user_id, state)
        raise


class _BoundedMaFileBuffer(io.BytesIO):
    def write(self, data: Buffer) -> int:
        if self.tell() + memoryview(data).nbytes > _MAX_NEW_MAFILE_BYTES:
            raise ValueError("maFile exceeds download limit")
        return super().write(data)


def _bounded_new_mafile_string(
    payload: dict,
    key: str,
    *,
    required: bool = False,
    max_length: int = _MAX_MAFILE_VALUE_LENGTH,
) -> str | None:
    value = payload.get(key)
    if value is None:
        if required:
            raise ValueError(f"missing {key}")
        return None
    if (
        not isinstance(value, str)
        or (required and not value)
        or len(value) > max_length
        or "\x00" in value
    ):
        raise ValueError(f"invalid {key}")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"invalid {key}") from exc
    return value


def _bounded_steamid(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is int:
        steamid = value
    elif (
        isinstance(value, str)
        and value.isascii()
        and value.isdecimal()
        and len(value) <= 20
    ):
        steamid = int(value)
    else:
        raise ValueError("invalid SteamID")
    if steamid <= 0 or steamid > 9_223_372_036_854_775_807:
        raise ValueError("invalid SteamID")
    return steamid


def _validated_new_mafile(payload: bytes) -> tuple[dict[str, str | None], dict]:
    if not payload or len(payload) > _MAX_NEW_MAFILE_BYTES:
        raise ValueError("invalid file size")
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("maFile root must be an object")

    values = {
        key: _bounded_new_mafile_string(
            raw,
            key,
            required=key == "shared_secret",
        )
        for key in _NEW_MAFILE_ENCRYPTED_FIELDS
    }
    values["account_name"] = _bounded_new_mafile_string(
        raw,
        "account_name",
        required=True,
        max_length=_MAX_ACCOUNT_NAME_LENGTH,
    )

    session_raw = raw.get("Session")
    if session_raw is None:
        session_raw = {}
    if not isinstance(session_raw, dict):
        raise ValueError("invalid Session")
    session_steamid = session_raw.get("SteamID")
    session_values = {
        "present": "Session" in raw,
        "access_token": _bounded_new_mafile_string(session_raw, "AccessToken"),
        "refresh_token": _bounded_new_mafile_string(session_raw, "RefreshToken"),
        "steamid": _bounded_steamid(
            session_steamid if session_steamid is not None else raw.get("steamid")
        ),
    }
    return values, session_values


@dp.message(
    NewMaFileImportFlow.waiting_document,
    F.document,
    NonCommandInputFilter(),
)
async def receive_new_mafile(message: Message, state: FSMContext) -> None:
    """Create or update an account from a locally validated maFile."""
    if message.chat.type != ChatType.PRIVATE:
        return
    if not await delete_secret_message_or_abort(message):
        return
    if message.from_user is None:
        return

    document = message.document
    if document is None:
        return
    file_name = (document.file_name or "").lower()
    if not file_name.endswith((".mafile", ".json")):
        await message.answer("❌ Файл должен иметь расширение .mafile или .json.")
        return
    if (
        type(document.file_size) is not int
        or document.file_size <= 0
        or document.file_size > _MAX_NEW_MAFILE_BYTES
    ):
        await message.answer("❌ Файл maFile пустой или превышает лимит 1 МБ.")
        return

    user_id = message.from_user.id
    status_message = await message.answer("🔄 Обрабатываю файл...")
    try:
        telegram_file = await bot.get_file(document.file_id)
        if not telegram_file.file_path:
            raise ValueError("missing Telegram file path")
        buffer = _BoundedMaFileBuffer()
        await bot.download_file(telegram_file.file_path, buffer)
        values, session_values = _validated_new_mafile(buffer.getvalue())
    except ValueError:
        await status_message.edit_text(
            "❌ maFile повреждён, слишком большой или содержит некорректные поля. "
            "Отправьте исправленный файл."
        )
        return
    except Exception:
        logger.exception("Failed to download new-account maFile for user %s", user_id)
        await status_message.edit_text(
            "❌ Не удалось безопасно прочитать файл. Попробуйте ещё раз."
        )
        return

    account_name = values["account_name"]
    if not isinstance(account_name, str):
        raise RuntimeError("validated account name is missing")

    created = False
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Mafile).where(
                Mafile.telegram_id == user_id,
                Mafile.account_name == account_name,
            )
        )
        mafile = result.scalar_one_or_none()
        if mafile is None:
            conflict_result = await session.execute(
                select(Mafile).where(Mafile.account_name == account_name)
            )
            if conflict_result.scalar_one_or_none() is not None:
                await status_message.edit_text(
                    "❌ Не удалось импортировать аккаунт. "
                    "Проверьте имя и повторите попытку."
                )
                return
            order_result = await session.execute(
                select(func.max(Mafile.sort_order)).where(Mafile.telegram_id == user_id)
            )
            max_order = order_result.scalar() or 0
            mafile = Mafile(
                telegram_id=user_id,
                account_name=account_name,
                password=encrypt_value(""),
                steamid=session_values["steamid"],
                fully_enrolled=True,
                unique_proxy=False,
                sort_order=max_order + 1,
                **{
                    field_name: encrypt_value(values[field_name])
                    for field_name in _NEW_MAFILE_ENCRYPTED_FIELDS
                },
            )
            session.add(mafile)
            created = True
        else:
            for field_name in _NEW_MAFILE_ENCRYPTED_FIELDS:
                setattr(mafile, field_name, encrypt_value(values[field_name]))
            mafile.fully_enrolled = True
            mafile.updated_at = datetime.now(UTC).replace(tzinfo=None)
            if session_values["steamid"] is not None:
                mafile.steamid = session_values["steamid"]

        if session_values["present"]:
            mafile.access_token = encrypt_value(session_values["access_token"])
            mafile.refresh_token = encrypt_value(session_values["refresh_token"])
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            await status_message.edit_text(
                "❌ Не удалось импортировать аккаунт. "
                "Проверьте имя и повторите попытку."
            )
            return

    await clear_user_workflows(user_id, state)
    action = "создан" if created else "обновлён"
    await status_message.edit_text(
        f"✅ <b>{escape(account_name)}</b> {action}!\n"
        "Steam Guard импортирован локально; запрос к Steam не выполнялся.\n"
        "⚠️ Для входа добавьте пароль в деталях аккаунта.",
        parse_mode="HTML",
    )


@dp.message(NewMaFileImportFlow.waiting_document, NonCommandInputFilter())
async def reject_new_mafile_non_document(
    message: Message,
    state: FSMContext,
) -> None:
    """Keep unrelated updates out of the legacy catch-all during import."""
    if message.chat.type != ChatType.PRIVATE:
        return
    await message.answer("❌ Отправьте файл .mafile или .json либо используйте /cancel.")

# ==================== НОВЫЕ КОМАНДЫ ДЛЯ УПРАВЛЕНИЯ ПРОКСИ ====================
# ВСТАВИТЬ: Эти команды добавить перед функцией main()

@dp.message(Command("set_proxy"))
async def cmd_set_proxy(message: Message, command: CommandObject):
    """
    Установка общего прокси для всех аккаунтов пользователя
    """
    if not await _prepare_secret_command(message):
        return

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
                f"🌐 <b>Прокси:</b> <code>{escape(str(redact_proxy_url(proxy_url)))}</code>\n\n"
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
    if not await _prepare_secret_command(message):
        return

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
            await message.answer(f"❌ Аккаунт <b>{escape(str(account_input))}</b> не найден", parse_mode="HTML")
            return

        # Устанавливаем уникальный прокси
        mafile.unique_proxy = True
        mafile.proxy = encrypt_value(proxy_url)
        await session.commit()

        await message.answer(
            f"✅ <b>Уникальный прокси установлен!</b>\n\n"
            f"📱 <b>Аккаунт:</b> <code>{escape(str(mafile.account_name))}</code>\n"
            f"🌐 <b>Прокси:</b> <code>{escape(str(redact_proxy_url(proxy_url)))}</code>\n\n"
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
            await message.answer(f"❌ Аккаунт <b>{escape(str(account_input))}</b> не найден", parse_mode="HTML")
            return

        # Удаляем уникальный прокси
        mafile.unique_proxy = False
        mafile.proxy = None
        await session.commit()

        await message.answer(
            f"✅ <b>Уникальный прокси удален!</b>\n\n"
            f"📱 <b>Аккаунт:</b> <code>{escape(str(mafile.account_name))}</code>\n\n"
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
            text += (
                f"✅ <code>"
                f"{escape(redact_proxy_url(decrypt_value(user.general_proxy) or ''))}"
                f"</code>\n"
            )
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
                    escaped_name = escape(str(mf.account_name))
                    redacted_proxy = escape(
                        redact_proxy_url(decrypt_value(mf.proxy) or "")
                    )
                    text += f"{idx}. <code>{escaped_name}</code>\n"
                    text += f"   🌐 <code>{redacted_proxy}</code>\n"
                    text += f"   💡 <code>/remove_account_proxy {escaped_name}</code>\n"

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