"""Import a maFile into an existing stored account (AccountMaFileImportFlow)."""

import asyncio
import io
import json
from html import escape
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from datetime import datetime, UTC
from guardbot.bot.filters import NonCommandInputFilter
from guardbot.bot.runtime import (
    bot,
    callback_feedback,
    clear_user_workflows,
    delete_secret_message_or_abort,
)
from guardbot.bot.states import AccountMaFileImportFlow
from guardbot.config import logger
from guardbot.database import AsyncSessionLocal
from guardbot.security import encrypt_value
from guardbot.services.ownership import get_owned_mafile
from guardbot.bot.handlers.callback_ids import (
    _MAX_DATABASE_INTEGER,
    _parse_positive_callback_id,
)

router = Router(name=__name__)


_MAX_ACCOUNT_MAFILE_BYTES = 1_048_576


_MAX_MAFILE_VALUE_LENGTH = 65_536


_ACCOUNT_MAFILE_ENCRYPTED_FIELDS = (
    "shared_secret",
    "identity_secret",
    "secret_1",
    "device_id",
    "serial_number",
    "revocation_code",
    "token_gid",
    "uri",
)


@router.callback_query(F.data.startswith("import_mafile_for_"))
async def callback_import_mafile_for(
    callback: CallbackQuery,
    state: FSMContext,
):
    """Start a typed maFile import for an existing account."""
    if not callback.message or callback.message.chat.type != ChatType.PRIVATE:
        await callback_feedback(
            callback,
            "❌ Импорт maFile доступен только в личном чате с ботом.",
            show_alert=True,
        )
        return

    mafile_id = _parse_positive_callback_id(callback.data, "import_mafile_for_")
    if mafile_id is None:
        await callback_feedback(callback, "Некорректный запрос", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)

        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        safe_account_name = escape(mafile.account_name)

    await callback_feedback(callback)
    await clear_user_workflows(callback.from_user.id, state)
    try:
        await state.set_state(AccountMaFileImportFlow.waiting_document)
        await state.set_data({"mafile_id": mafile_id})
        await callback.message.edit_text(
            "📤 <b>ИМПОРТ STEAM GUARD</b>\n\n"
            f"Аккаунт: <b>{safe_account_name}</b>\n\n"
            "Отправьте файл <b>.mafile</b> с ключами Steam Guard.\n\n"
            "<b>Где взять?</b>\n"
            "• Steam Desktop Authenticator (SDA)\n"
            "• Мобильный Steam (извлечь)\n\n"
            "Отправьте файл или /cancel для отмены.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=f"account_detail_{mafile_id}",
                    )]
                ]
            ),
        )
    except (Exception, asyncio.CancelledError):
        await clear_user_workflows(callback.from_user.id, state)
        raise


def _bounded_mafile_string(
    payload: dict,
    key: str,
    *,
    required: bool = False,
) -> str | None:
    value = payload.get(key)
    if value is None:
        if required:
            raise ValueError(f"missing {key}")
        return None
    if (
        not isinstance(value, str)
        or (required and not value)
        or len(value) > _MAX_MAFILE_VALUE_LENGTH
    ):
        raise ValueError(f"invalid {key}")
    return value


def _validated_account_mafile(payload: bytes) -> tuple[dict[str, str | None], dict]:
    if not payload or len(payload) > _MAX_ACCOUNT_MAFILE_BYTES:
        raise ValueError("invalid file size")
    try:
        decoded = payload.decode("utf-8")
        raw = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("maFile root must be an object")

    account_name = _bounded_mafile_string(raw, "account_name", required=True)
    values = {
        key: _bounded_mafile_string(raw, key, required=(key == "shared_secret"))
        for key in _ACCOUNT_MAFILE_ENCRYPTED_FIELDS
    }
    values["account_name"] = account_name

    session_data = raw.get("Session")
    if session_data is None:
        session_data = {}
    if not isinstance(session_data, dict):
        raise ValueError("invalid Session")
    session_values = {
        "access_token": _bounded_mafile_string(session_data, "AccessToken"),
        "refresh_token": _bounded_mafile_string(session_data, "RefreshToken"),
        "steamid": None,
        "present": "Session" in raw,
    }
    raw_steamid = session_data.get("SteamID")
    if raw_steamid is not None:
        if type(raw_steamid) is int:
            steamid = raw_steamid
        elif (
            isinstance(raw_steamid, str)
            and raw_steamid.isascii()
            and raw_steamid.isdecimal()
            and len(raw_steamid) <= 20
        ):
            steamid = int(raw_steamid)
        else:
            raise ValueError("invalid SteamID")
        if steamid <= 0 or steamid > 9_223_372_036_854_775_807:
            raise ValueError("invalid SteamID")
        session_values["steamid"] = steamid
    return values, session_values


@router.message(AccountMaFileImportFlow.waiting_document, NonCommandInputFilter())
async def receive_account_mafile(message: Message, state: FSMContext) -> None:
    """Import maFile secrets locally for the selected owned account."""
    if message.chat.type != ChatType.PRIVATE:
        return
    if not await delete_secret_message_or_abort(message):
        return

    state_data = await state.get_data()
    mafile_id = state_data.get("mafile_id")
    if (
        set(state_data) != {"mafile_id"}
        or type(mafile_id) is not int
        or mafile_id <= 0
        or mafile_id > _MAX_DATABASE_INTEGER
    ):
        await clear_user_workflows(message.from_user.id, state)
        await message.answer("❌ Данные импорта повреждены. Начните импорт заново.")
        return

    document = message.document
    if not document:
        await message.answer("❌ Отправьте файл .mafile или .json.")
        return
    file_name = (document.file_name or "").lower()
    if not file_name.endswith((".mafile", ".json")):
        await message.answer("❌ Файл должен иметь расширение .mafile или .json.")
        return
    if (
        type(document.file_size) is not int
        or document.file_size <= 0
        or document.file_size > _MAX_ACCOUNT_MAFILE_BYTES
    ):
        await message.answer("❌ Файл maFile пустой или превышает лимит 1 МБ.")
        return

    status_message = await message.answer("🔄 Обрабатываю файл...")
    try:
        telegram_file = await bot.get_file(document.file_id)
        if not telegram_file.file_path:
            raise ValueError("missing Telegram file path")
        buffer = io.BytesIO()
        await bot.download_file(telegram_file.file_path, buffer)
        payload = buffer.getvalue()
        values, session_values = _validated_account_mafile(payload)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        await status_message.edit_text(
            "❌ maFile повреждён, слишком большой или содержит некорректные поля. "
            "Отправьте исправленный файл."
        )
        return
    except Exception:
        logger.exception("Failed to download account maFile for user %s", message.from_user.id)
        await status_message.edit_text(
            "❌ Не удалось безопасно прочитать файл. Попробуйте ещё раз."
        )
        return

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, message.from_user.id)
        if not mafile:
            await clear_user_workflows(message.from_user.id, state)
            await status_message.edit_text("❌ Аккаунт не найден")
            return
        if values["account_name"] != mafile.account_name:
            await status_message.edit_text(
                "❌ В maFile указан другой аккаунт. Ожидается файл для "
                f"<code>{escape(mafile.account_name)}</code>.",
                parse_mode="HTML",
            )
            return

        for field_name in _ACCOUNT_MAFILE_ENCRYPTED_FIELDS:
            setattr(mafile, field_name, encrypt_value(values[field_name]))
        if session_values["present"]:
            mafile.access_token = encrypt_value(session_values["access_token"])
            mafile.refresh_token = encrypt_value(session_values["refresh_token"])
            if session_values["steamid"] is not None:
                mafile.steamid = session_values["steamid"]
        mafile.fully_enrolled = True
        mafile.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await session.commit()
        await clear_user_workflows(message.from_user.id, state)

        await status_message.edit_text(
            "✅ <b>Steam Guard успешно импортирован!</b>\n\n"
            f"📱 <b>Аккаунт:</b> <code>{escape(mafile.account_name)}</code>\n\n"
            "Коды Steam Guard рассчитываются локально; сетевой запрос к Steam "
            "при импорте не выполнялся.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="🔐 Получить код",
                        callback_data=f"action_get_code_{mafile.id}",
                    )],
                    [InlineKeyboardButton(
                        text="👤 К аккаунту",
                        callback_data=f"account_detail_{mafile.id}",
                    )],
                ]
            ),
        )
