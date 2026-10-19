"""Steam Guard / email challenge code entry for pending logins."""

import asyncio
from html import escape
from typing import cast
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
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
    clear_user_workflows,
    close_owned_resource,
    delete_secret_message_or_abort,
)
from guardbot.bot.states import SteamChallengeFlow
from guardbot.config import logger
from guardbot.database import AsyncSessionLocal
from guardbot.services.ownership import get_owned_mafile
from guardbot.services.session_manager import SteamSessionManager
from guardbot.steam.client import InvalidSteamGuardCode, ProxyRequiredError

router = Router(name=__name__)


# Pending Steam login challenges accepted by SteamChallengeFlow.waiting_code:
# manual_mobile - "enter code manually" button; email - code from Steam email;
# mobile - password login on an account whose Guard keys the bot does not have.
_STEAM_CHALLENGE_KINDS = frozenset({"manual_mobile", "email", "mobile"})


@router.callback_query(F.data.startswith("manual_code_"))
@validated_callback_data(r"manual_code_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_manual_code(callback: CallbackQuery, state: FSMContext):
    """Ручной ввод Steam Guard кода"""
    mafile_id = int(callback.data.split("_")[2])
    telegram_id = callback.from_user.id

    if not callback.message or callback.message.chat.type != ChatType.PRIVATE:
        await callback_feedback(
            callback,
            "❌ Ввод Steam Guard кода доступен только в личном чате с ботом.",
            show_alert=True,
        )
        return
    callback_message = cast(Message, callback.message)

    client = None
    try:
        async with AsyncSessionLocal() as session:
            mafile = await get_owned_mafile(
                session,
                mafile_id,
                callback.from_user.id,
            )

            if not mafile:
                await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
                return

            await callback_feedback(callback)
            await clear_user_workflows(telegram_id, state)
            client = await SteamSessionManager.create_steam_client_from_db(
                mafile,
                session,
            )
    except ProxyRequiredError:
        await callback_message.edit_text(
            "❌ Для входа в Steam сначала настройте общий прокси или "
            "уникальный прокси аккаунта."
        )
        return
    except asyncio.CancelledError:
        if client is not None:
            await close_owned_resource(client)
        raise
    except Exception:
        if client is not None:
            await close_owned_resource(client)
        logger.exception(
            "Failed to create Steam client for manual challenge user_id=%s",
            telegram_id,
        )
        await callback_message.edit_text(
            "❌ Не удалось начать вход в Steam. Проверьте настройки аккаунта."
        )
        return

    client_owned_by_handler = True
    challenge_id = None

    try:
        await client.login()
        client_owned_by_handler = False
        challenge_id = await publish_pending_challenge(
            user_id=telegram_id,
            mafile_id=mafile_id,
            kind="manual_mobile",
            client=client,
            state=state,
            fsm_state=SteamChallengeFlow.waiting_code,
        )

        await callback_message.edit_text(
            f"📱 <b>Введите Steam Guard код для {escape(str(mafile.account_name))}:</b>\n\n"
            "Код из приложения Steam / SDA",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=f"account_detail_{mafile.id}",
                    )]
                ]
            )
        )
    except (TelegramAPIError, asyncio.CancelledError):
        if client_owned_by_handler:
            await close_owned_resource(client)
        elif challenge_id is not None:
            await clear_owned_pending_challenge(
                challenge_id,
                state,
                user_id=telegram_id,
            )
        raise
    except Exception:
        if client_owned_by_handler:
            await close_owned_resource(client)
        elif challenge_id is not None:
            await clear_owned_pending_challenge(
                challenge_id,
                state,
                user_id=telegram_id,
            )
        await callback_message.edit_text("❌ Не удалось начать ввод Steam Guard кода.")


@router.message(
    SteamChallengeFlow.waiting_code,
    F.text,
    NonCommandInputFilter(),
)
async def receive_manual_steam_code(message: Message, state: FSMContext) -> None:
    """Consume a Steam login code (app, SDA or email) without persisting it."""
    if message.chat.type != ChatType.PRIVATE or message.from_user is None:
        return

    user_id = message.from_user.id
    challenge_snapshot = current_pending_challenge_token(user_id)
    try:
        if not await delete_secret_message_or_abort(message):
            return
    except BaseException:
        if challenge_snapshot is not None:
            await clear_owned_pending_challenge(
                challenge_snapshot,
                state,
                user_id=user_id,
            )
        raise

    data = await read_pending_challenge_data(user_id, state)
    challenge_id = data.get("challenge_id")
    mafile_id = data.get("mafile_id")
    kind = data.get("kind")
    valid_challenge_id = (
        isinstance(challenge_id, str)
        and len(challenge_id) == 32
        and all(character in "0123456789abcdef" for character in challenge_id)
    )
    valid_mafile_id = (
        type(mafile_id) is int
        and 0 < mafile_id <= 2_147_483_647
    )
    if (
        not valid_challenge_id
        or not valid_mafile_id
        or not isinstance(kind, str)
        or kind not in _STEAM_CHALLENGE_KINDS
    ):
        if challenge_snapshot is not None:
            await clear_owned_pending_challenge(
                challenge_snapshot,
                state,
                user_id=user_id,
            )
        else:
            await state.clear()
        await message.answer("⌛ Запрос кода устарел. Начните вход заново.")
        return
    challenge_id = cast(str, challenge_id)
    mafile_id = cast(int, mafile_id)
    kind = cast(str, kind)

    code = message.text
    if not code or len(code) > 64 or not code.isascii():
        try:
            await message.answer("❌ Неверный формат кода. Попробуйте ещё раз.")
        except BaseException:
            await clear_owned_pending_challenge(
                challenge_id,
                state,
                user_id=user_id,
            )
            raise
        return

    try:
        challenge = await lease_pending_challenge(
            challenge_id,
            user_id=user_id,
            mafile_id=mafile_id,
            kind=kind,
        )
    except BaseException:
        # The lease was never granted to this handler. If another handler owns
        # the client, it is responsible for cleanup; never close it from here.
        if not pending_challenge_is_leased(challenge_id):
            await clear_owned_pending_challenge(
                challenge_id,
                state,
                user_id=user_id,
            )
        raise
    if challenge is None:
        if pending_challenge_is_leased(challenge_id):
            await message.answer(
                "⏳ Предыдущий код ещё проверяется. Дождитесь результата."
            )
            return
        await clear_owned_pending_challenge(
            challenge_id,
            state,
            user_id=user_id,
        )
        await message.answer("⌛ Запрос кода устарел. Начните вход заново.")
        return

    try:
        status_message = await message.answer("🔄 Проверяю код...")
    except BaseException:
        await clear_owned_pending_challenge(
            challenge_id,
            state,
            user_id=user_id,
            expected_entry=challenge,
        )
        raise

    try:
        await challenge.client.confirm_login(code)

        async with AsyncSessionLocal() as session:
            owned_mafile = await get_owned_mafile(session, mafile_id, user_id)
            if owned_mafile is None:
                await clear_owned_pending_challenge(
                    challenge_id,
                    state,
                    user_id=user_id,
                    expected_entry=challenge,
                )
                await status_message.edit_text("❌ Аккаунт не найден.")
                return
            session_saved = await SteamSessionManager.save_steam_session_to_db(
                session,
                mafile_id,
                challenge.client,
                telegram_id=user_id,
            )
            if not session_saved:
                await clear_owned_pending_challenge(
                    challenge_id,
                    state,
                    user_id=user_id,
                    expected_entry=challenge,
                )
                await status_message.edit_text("❌ Аккаунт не найден.")
                return

        has_shared_secret = bool(challenge.client.shared_secret)
        await clear_owned_pending_challenge(
            challenge_id,
            state,
            user_id=user_id,
            expected_entry=challenge,
        )
        if kind == "email":
            await status_message.edit_text(
                "✅ <b>Вход выполнен успешно!</b>\n\n"
                "⚠️ <b>Рекомендуется подключить мобильный Steam Guard</b>\n"
                "Это повысит безопасность и позволит боту генерировать коды.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="🔐 Подключить Steam Guard",
                            callback_data=f"setup_steamguard_{mafile_id}",
                        )],
                        [InlineKeyboardButton(
                            text="📋 К аккаунту",
                            callback_data=f"account_detail_{mafile_id}",
                        )],
                    ]
                ),
            )
        elif kind == "mobile":
            await status_message.edit_text(
                "✅ <b>Вход выполнен успешно!</b>\n\n"
                "🔐 Steam Guard на аккаунте включён, но ключей у бота нет. "
                "Импортируйте maFile, чтобы бот сам генерировал коды.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="📤 Импортировать mafile",
                            callback_data=f"import_mafile_for_{mafile_id}",
                        )],
                        [InlineKeyboardButton(
                            text="📋 К аккаунту",
                            callback_data=f"account_detail_{mafile_id}",
                        )],
                    ]
                ),
            )
        elif has_shared_secret:
            await status_message.edit_text(
                "✅ <b>Вход выполнен успешно!</b>\n\n"
                "🔐 Секрет Steam Guard сохранён. Теперь коды можно получать в боте.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="📋 К аккаунту",
                            callback_data=f"account_detail_{mafile_id}",
                        )]
                    ]
                ),
            )
        else:
            await status_message.edit_text(
                "✅ <b>Вход выполнен!</b>\n\n"
                "⚠️ Секрет Steam Guard не получен. Возможно, мобильный "
                "аутентификатор не активирован.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="📋 К аккаунту",
                            callback_data=f"account_detail_{mafile_id}",
                        )]
                    ]
                ),
            )
    except InvalidSteamGuardCode:
        try:
            await status_message.edit_text(
                "❌ Неверный код подтверждения. Попробуйте ещё раз."
            )
        except BaseException:
            await clear_owned_pending_challenge(
                challenge_id,
                state,
                user_id=user_id,
                expected_entry=challenge,
            )
            raise
        try:
            await release_pending_challenge(challenge_id, challenge)
        except BaseException:
            await clear_owned_pending_challenge(
                challenge_id,
                state,
                user_id=user_id,
                expected_entry=challenge,
            )
            raise
    except asyncio.CancelledError:
        await clear_owned_pending_challenge(
            challenge_id,
            state,
            user_id=user_id,
            expected_entry=challenge,
        )
        raise
    except Exception:
        logger.exception("Manual Steam Guard challenge failed for user_id=%s", user_id)
        await clear_owned_pending_challenge(
            challenge_id,
            state,
            user_id=user_id,
            expected_entry=challenge,
        )
        await status_message.edit_text(
            "❌ Не удалось подтвердить код. Начните вход заново."
        )


@router.message(SteamChallengeFlow.waiting_code, F.document, NonCommandInputFilter())
async def reject_challenge_document(message: Message, state: FSMContext) -> None:
    """A file sent at the code prompt may be a maFile: delete it, keep the challenge.

    maFile import has a single hardened path (the "Import mafile" button) with
    size limits and strict validation; this prompt only accepts the code text.
    """
    if message.chat.type != ChatType.PRIVATE or message.from_user is None:
        return
    if not await delete_secret_message_or_abort(message):
        return
    await message.answer(
        "🔐 Файл удалён из чата и не обработан.\n\n"
        "Сейчас бот ждёт код подтверждения — отправьте его обычным сообщением.\n"
        "Чтобы загрузить maFile, нажмите «📤 Импортировать mafile» под запросом "
        "кода или в карточке аккаунта."
    )
