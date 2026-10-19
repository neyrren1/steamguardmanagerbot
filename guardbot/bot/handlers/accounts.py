"""Extracted from the legacy bot module without behavior changes."""

import asyncio
import io
import json
from contextlib import suppress
from html import escape
from typing import cast

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from datetime import UTC, datetime
from guardbot.bot.callback_validation import validated_callback_data
from guardbot.bot.filters import NonCommandInputFilter
from guardbot.bot.pending_challenges import (
    clear_owned_pending_challenge,
    current_pending_challenge_token,
    lease_pending_challenge,
    pending_challenge_is_leased,
    publish_pending_challenge,
    read_pending_challenge_data,
    release_pending_challenge,
)
from guardbot.bot.runtime import (
    callback_feedback,
    check_proxy_available,
    clear_user_workflows,
    close_owned_resource,
    delete_secret_message_or_abort,
    bot,
    require_proxy,
)
from guardbot.bot.states import (
    AccountAddFlow,
    AccountMaFileImportFlow,
    BulkImportFlow,
    LoginPasswordFlow,
    NoteFlow,
    ProxyFlow,
    SteamChallengeFlow,
)
from guardbot.config import STEAM_CURRENCIES, logger
from guardbot.database import AccountGroup, AsyncSessionLocal, Mafile, User
from guardbot.security import decrypt_value, encrypt_value
from guardbot.services.ownership import get_owned_group, get_owned_mafile
from guardbot.services.session_manager import SteamSessionManager
from guardbot.steam.client import AsyncSteamMobile, InvalidCredentials, InvalidSteamGuardCode, LoginConfirmType, ProxyRequiredError, generate_device_id, parse_proxy_string, probe_proxy_external_ip, redact_proxy_url
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
import time

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


@router.callback_query(F.data.startswith("action_check_token_"))
@validated_callback_data(r"action_check_token_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_action_check_token(callback: CallbackQuery):
    """Проверка токена по кнопке"""
    mafile_id = int(callback.data.split("_")[3])

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)

        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        await callback_feedback(callback, "Проверяю токен...")

        if not mafile.refresh_token:
            await callback.message.answer(
                f"⚠️ <b>{escape(str(mafile.account_name))}</b>: Нет refresh token",
                parse_mode="HTML"
            )
            return

        password = decrypt_value(mafile.password) if mafile.password else ""
        client = AsyncSteamMobile(mafile.account_name, password)

        try:
            refresh_token = decrypt_value(mafile.refresh_token)
            is_expired = client.is_token_expired(refresh_token)

            if is_expired:
                await callback.message.answer(
                    f"❌ <b>{escape(str(mafile.account_name))}</b>: Токен истек\n"
                    f"Требуется повторный вход: нажмите кнопку «Войти в аккаунт»",
                    parse_mode="HTML"
                )
            else:
                expire_time = datetime.fromtimestamp(
                    client.get_token_expire_timestamp(refresh_token)
                )
                days_left = (expire_time - datetime.utcnow()).days

                await callback.message.answer(
                    f"✅ <b>{escape(str(mafile.account_name))}</b>: Токен действителен\n"
                    f"Истекает: {expire_time.strftime('%d.%m.%Y %H:%M')}\n"
                    f"Осталось дней: {days_left}",
                    parse_mode="HTML"
                )
        except Exception as e:
            await callback.message.answer(
                f"⚠️ <b>{escape(str(mafile.account_name))}</b>: Не удалось проверить токен\n"
                f"<code>{escape(str(e))}</code>",
                parse_mode="HTML"
            )
        finally:
            await client.close()

@router.callback_query(F.data.startswith("delete_account_"))
@validated_callback_data(r"delete_account_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_delete_account(callback: CallbackQuery):
    mafile_id = int(callback.data.split("_")[2])

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data="confirm_delete_" + str(mafile_id)),
                InlineKeyboardButton(text="❌ Отмена", callback_data="account_detail_" + str(mafile_id))
            ]
        ]
    )

    await callback.message.edit_text(
        "⚠️ <b>ПОДТВЕРЖДЕНИЕ УДАЛЕНИЯ</b>\n\n"
        "Вы действительно хотите удалить этот аккаунт?\n"
        "Это действие <b>НЕВОЗМОЖНО</b> отменить!",
        parse_mode="HTML",
        reply_markup=keyboard
    )
    await callback_feedback(callback)

@router.callback_query(F.data.startswith("confirm_delete_"))
@validated_callback_data(r"confirm_delete_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_confirm_delete(callback: CallbackQuery):
    await callback_feedback(callback)
    mafile_id = int(callback.data.split("_")[2])

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)

        if mafile:
            account_name = mafile.account_name
            await session.delete(mafile)
            await session.commit()

            await callback.message.edit_text(
                f"✅ Аккаунт <b>{escape(str(account_name))}</b> успешно удален!",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="📋 К списку аккаунтов", callback_data="my_accounts")],
                        [InlineKeyboardButton(text="🏠 На главную", callback_data="back_to_start")]
                    ]
                )
            )
        else:
            await callback.message.edit_text("❌ Аккаунт не найден")

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

@router.message(Command("my_accounts"))
@router.message(Command("accounts"))
@router.message(Command("list"))
async def cmd_my_accounts(message: Message):
    """Показывает список аккаунтов с пагинацией (через сообщение)"""
    telegram_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.telegram_id == telegram_id).order_by(Mafile.account_name)
        result = await session.execute(stmt)
        all_accounts = result.scalars().all()

        if not all_accounts:
            await message.answer(
                "❌ У вас нет сохраненных Steam аккаунтов\n"
                "Добавьте аккаунт: /add"
            )
            return

        # Показываем первые 10 аккаунтов в текстовом виде + кнопку для открытия меню
        accounts_text = f"📋 <b>Ваши Steam аккаунты (всего {len(all_accounts)}):</b>\n\n"

        for idx, mf in enumerate(all_accounts[:10], 1):
            has_full_login = bool(mf.access_token and mf.refresh_token and mf.password and mf.password.strip())
            status = "✅" if has_full_login else "⚠️"
            guard = "🔐" if mf.shared_secret else "❌"
            accounts_text += (
                f"{idx}. {status} {guard} <code>{escape(str(mf.account_name))}</code>\n"
            )

        if len(all_accounts) > 10:
            accounts_text += f"\n<i>... и еще {len(all_accounts) - 10} аккаунтов</i>"

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="👤 Открыть список аккаунтов",
                    callback_data="my_accounts"
                )]
            ]
        )

        await message.answer(accounts_text, parse_mode="HTML", reply_markup=keyboard)

@router.message(Command("get_code"))
@router.message(Command("getcode"))
@router.message(Command("guardcode"))
@router.message(Command("guard_code"))
@router.message(Command("gc"))
async def cmd_get_code_aliases(message: Message, command: CommandObject):
    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(
            Mafile.telegram_id == telegram_id,
            Mafile.shared_secret.isnot(None)
        )
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await message.answer(
                "❌ У вас нет аккаунтов с настроенным Steam Guard\n"
                "Добавьте аккаунт: /add"
            )
            return

        if not args:
            # Красивый список аккаунтов с кнопками
            keyboard_buttons = []
            row = []
            for idx, mf in enumerate(mafiles, 1):
                row.append(InlineKeyboardButton(
                    text=f"{idx}. {mf.account_name}",
                    callback_data=f"get_code_{mf.id}"
                ))
                if len(row) == 2:
                    keyboard_buttons.append(row)
                    row = []
            if row:
                keyboard_buttons.append(row)

            keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

            await message.answer(
                "📋 <b>Выберите аккаунт для получения кода:</b>",
                parse_mode="HTML",
                reply_markup=keyboard
            )
            return

        account_input = args[0]
        mafile = None

        if account_input.isdigit():
            idx = int(account_input) - 1
            if 0 <= idx < len(mafiles):
                mafile = mafiles[idx]
        else:
            mafile = next((mf for mf in mafiles if mf.account_name.lower() == account_input.lower()), None)

        if not mafile:
            await message.answer(
                f"❌ Аккаунт <b>{escape(str(account_input))}</b> не найден или не имеет Steam Guard",
                parse_mode="HTML"
            )
            return

        await generate_and_send_code(message, mafile)


@router.callback_query(F.data.startswith("refresh_code_"))
@validated_callback_data(r"refresh_code_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_refresh_code(callback: CallbackQuery):
    """Обновление кода (редактирует существующее сообщение)"""
    mafile_id = int(callback.data.split("_")[2])

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)

        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        await callback_feedback(callback, "Обновляю...")

        # 🔥 Получаем и расшифровываем прокси
        proxy = None
        if mafile.unique_proxy and mafile.proxy:
            proxy = decrypt_value(mafile.proxy)
        else:
            stmt = select(User).where(User.telegram_id == mafile.telegram_id)
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()
            if user and user.general_proxy:
                proxy = decrypt_value(user.general_proxy)

        # 🔥 Создаем клиент с расшифрованным паролем и прокси
        password = decrypt_value(mafile.password) if mafile.password else ""
        client = AsyncSteamMobile(mafile.account_name, password, proxy=proxy)

        try:
            # Загружаем мобильные данные (расшифровывая)
            mobile_data = {
                'shared_secret': decrypt_value(mafile.shared_secret),
                'device_id': decrypt_value(mafile.device_id) or generate_device_id(),
                'identity_secret': decrypt_value(mafile.identity_secret),
                'serial_number': decrypt_value(mafile.serial_number),
                'revocation_code': decrypt_value(mafile.revocation_code),
                'uri': decrypt_value(mafile.uri),
                'token_gid': decrypt_value(mafile.token_gid),
                'secret_1': decrypt_value(mafile.secret_1)
            }
            client.load_mobile(mobile_data)

            # Пробуем синхронизировать время
            try:
                await client.align_time()
            except Exception as e:
                logger.warning(f"Ошибка синхронизации при обновлении: {e}")

            code = client.generate_steam_guard_code()

            if not code:
                await callback.message.edit_text(
                    "❌ Не удалось сгенерировать код. Проверьте shared_secret.",
                    parse_mode="HTML"
                )
                return

            # Вычисляем оставшееся время
            local_time = int(time.time())
            remaining_seconds = 30 - (local_time % 30)

            # Прогресс-бар
            filled = remaining_seconds // 2
            empty = 15 - filled
            progress_bar = "▰" * filled + "▱" * empty

            # Источник времени
            time_source = "Steam" if client.is_using_steam_time() else "локальное"

            message_text = (
                f"<b>🔐 STEAM GUARD КОД</b>\n\n"
                f"<b>📱 Аккаунт:</b> <code>{escape(str(mafile.account_name))}</code>\n"
                f"<b>🔑 Код:</b> <code>{code}</code>\n\n"
                f"<code>[{progress_bar}]</code> обновится через <b>{remaining_seconds}с</b>\n"
                f"<i>⏰ Время: {time_source}</i>"
            )

            # Сохраняем ту же клавиатуру
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="🔄 Обновить код",
                        callback_data=f"refresh_code_{mafile.id}"
                    )]
                ]
            )

            await callback.message.edit_text(message_text, parse_mode="HTML", reply_markup=keyboard)

        except Exception as e:
            await callback_feedback(callback, f"Ошибка: {str(e)}", show_alert=True)
            logger.error(f"Ошибка обновления кода для {mafile.account_name}: {e}")
        finally:
            await client.close()

@router.message(Command("login"))
@router.message(Command("signin"))
@require_proxy
async def cmd_login(
    message: Message,
    command: CommandObject,
    state: FSMContext | None = None,
    **kwargs,
):
    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id

    if not args:
        await message.answer("❌ Использование: /login [account_name или номер]")
        return

    # 🔥 Удаляем сообщение с командой (может содержать чувствительные данные)
    if len(args) > 1:  # Если есть доп. аргументы (возможно пароль)
        try:
            await message.delete()
        except:
            pass

    account_input = args[0]

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.telegram_id == telegram_id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await message.answer("❌ У вас нет сохраненных Steam аккаунтов")
            return

        mafile = None

        if account_input.isdigit():
            idx = int(account_input) - 1
            if 0 <= idx < len(mafiles):
                mafile = mafiles[idx]
        else:
            mafile = next((mf for mf in mafiles if mf.account_name.lower() == account_input.lower()), None)

        if not mafile:
            await message.answer(f"❌ Аккаунт <b>{escape(str(account_input))}</b> не найден", parse_mode="HTML")
            return

        if state is None:
            raise RuntimeError("FSM context is required for Steam login")
        await perform_login_full(
            message,
            mafile,
            session,
            telegram_id=telegram_id,
            state=state,
        )


@router.message(Command("check_tokens"))
@router.message(Command("tokens"))
async def cmd_check_tokens(message: Message):
    telegram_id = message.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.telegram_id == telegram_id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await message.answer("❌ У вас нет сохраненных Steam аккаунтов")
            return

        status_text = "🔍 <b>Статус токенов:</b>\n\n"

        for mf in mafiles:
            status_text += f"<b>{escape(str(mf.account_name))}</b>:\n"

            if not mf.refresh_token:
                status_text += "  ⚠️ Нет refresh token\n"
            else:
                password = decrypt_value(mf.password) if mf.password else ""
                client = AsyncSteamMobile(mf.account_name, password)
                try:
                    refresh_token = decrypt_value(mf.refresh_token)
                    is_expired = client.is_token_expired(refresh_token)
                    if is_expired:
                        status_text += "  ❌ Токен истек\n"
                    else:
                        expire_time = datetime.fromtimestamp(
                            client.get_token_expire_timestamp(refresh_token)
                        )
                        status_text += "  ✅ Токен действителен до " + expire_time.strftime('%Y-%m-%d %H:%M') + "\n"
                except:
                    status_text += "  ⚠️ Не удалось проверить токен\n"
                finally:
                    await client.close()

            if mf.shared_secret:
                status_text += "  🔐 Steam Guard активирован\n"

            status_text += "\n"

        await message.answer(status_text, parse_mode="HTML")

@router.message(Command("delete_account"))
@router.message(Command("delete"))
@router.message(Command("remove"))
async def cmd_delete_account(message: Message, command: CommandObject):
    args = command.args.split() if command.args else []
    telegram_id = message.from_user.id

    if not args:
        await message.answer("❌ Использование: /delete [account_name или номер]")
        return

    account_input = args[0]

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.telegram_id == telegram_id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await message.answer("❌ У вас нет сохраненных Steam аккаунтов")
            return

        mafile = None

        if account_input.isdigit():
            idx = int(account_input) - 1
            if 0 <= idx < len(mafiles):
                mafile = mafiles[idx]
        else:
            mafile = next((mf for mf in mafiles if mf.account_name == account_input), None)

        if not mafile:
            await message.answer(
                f"❌ Аккаунт <b>{escape(str(account_input))}</b> не найден",
                parse_mode="HTML",
            )
            return

        account_name = mafile.account_name
        await session.delete(mafile)
        await session.commit()

        await message.answer(
            f"✅ Аккаунт <b>{escape(str(account_name))}</b> удален",
            parse_mode="HTML",
        )

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    """Отмена текущего действия"""
    user_id = message.from_user.id
    middleware_snapshot = getattr(
        message,
        "_guardbot_cancelled_workflow",
        None,
    )
    if isinstance(middleware_snapshot, dict):
        current_state = middleware_snapshot.get("typed_state")
        had_workflow = bool(middleware_snapshot.get("had_workflow"))
    else:
        current_state = await state.get_state()
        had_workflow = bool(
            current_state
            or current_pending_challenge_token(user_id) is not None
        )

    await clear_user_workflows(user_id, state)

    if current_state == AccountAddFlow.waiting_credentials.state:
        await message.answer(
            "❌ Добавление аккаунта отменено.\n"
            "Используйте /accounts для просмотра списка."
        )
    elif current_state == NoteFlow.waiting_note.state:
        await message.answer("❌ Изменение заметки отменено.")
    elif current_state == ProxyFlow.waiting_proxy.state:
        await message.answer("❌ Настройка прокси отменена.")
    elif current_state == BulkImportFlow.waiting_credentials.state:
        await message.answer("❌ Массовый импорт отменен.")
    elif had_workflow:
        await message.answer("❌ Действие отменено.")
    else:
        await message.answer("Нет активных действий для отмены.")


from guardbot.bot.handlers.callback_ids import (  # noqa: E402,F401 - compatibility exports
    _MAX_DATABASE_INTEGER,
    _parse_nonnegative_callback_id,
    _parse_positive_callback_id,
)


from guardbot.bot.handlers.menus import (  # noqa: E402,F401 - compatibility exports
    callback_info,
    callback_instruction,
    callback_mafile_instruction,
    callback_settings,
    update_settings_message,
)


from guardbot.bot.handlers.account_list import (  # noqa: E402,F401 - compatibility exports
    callback_accounts_page,
    callback_my_accounts,
    callback_page_info,
    show_accounts_page,
)


from guardbot.bot.handlers.account_settings import (  # noqa: E402,F401 - compatibility exports
    callback_account_detail,
    callback_account_settings,
    callback_change_note,
    callback_toggle_unique_proxy,
    process_account_note,
    reject_note_non_text,
    update_account_settings_message,
)


from guardbot.bot.handlers.account_proxy import (  # noqa: E402,F401 - compatibility exports
    callback_cancel_state_to_settings,
    callback_change_unique_proxy,
    callback_remove_account_proxy,
    callback_set_unique_proxy,
    callback_test_account_proxy,
    receive_unique_proxy,
    _start_unique_proxy_prompt,
)


from guardbot.bot.handlers.guard_codes import (  # noqa: E402,F401 - compatibility exports
    callback_action_get_code,
    generate_and_send_code,
)


from guardbot.bot.handlers.account_login import (  # noqa: E402,F401 - compatibility exports
    callback_action_login,
    callback_enter_password,
    callback_try_refresh,
    perform_login_full,
    receive_login_password,
    reject_login_password_non_text,
)


from guardbot.bot.handlers.steam_challenge import (  # noqa: E402,F401 - compatibility exports
    callback_manual_code,
    receive_manual_steam_code,
    reject_challenge_document,
    _STEAM_CHALLENGE_KINDS,
)
