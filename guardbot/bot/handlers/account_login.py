"""Password login, password prompt and token-refresh login for a stored account."""

import asyncio
from html import escape
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
    publish_pending_challenge,
)
from guardbot.bot.runtime import (
    callback_feedback,
    check_proxy_available,
    clear_user_workflows,
    close_owned_resource,
    delete_secret_message_or_abort,
)
from guardbot.bot.states import LoginPasswordFlow, SteamChallengeFlow
from guardbot.config import logger, STEAM_CURRENCIES
from guardbot.database import AsyncSessionLocal, Mafile
from guardbot.security import decrypt_value, encrypt_value
from guardbot.services.ownership import get_owned_mafile
from guardbot.services.session_manager import SteamSessionManager
from guardbot.steam.client import (
    generate_device_id,
    InvalidCredentials,
    InvalidSteamGuardCode,
    LoginConfirmType,
    ProxyRequiredError,
)
from sqlalchemy.ext.asyncio import AsyncSession
from guardbot.bot.handlers.callback_ids import _MAX_DATABASE_INTEGER

router = Router(name=__name__)


@router.callback_query(F.data.startswith("action_login_"))
@validated_callback_data(r"action_login_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_action_login(callback: CallbackQuery, state: FSMContext):
    """Вход в аккаунт по паролю (полный вход)"""
    mafile_id = int(callback.data.split("_")[2])
    telegram_id = callback.from_user.id

    # Проверяем наличие прокси
    has_proxy, _ = await check_proxy_available(telegram_id, for_steam_requests=True)
    if not has_proxy:
        await callback_feedback(callback, "❌ Нет доступного прокси!", show_alert=True)
        await callback.message.answer(
            "❌ <b>Нет доступного прокси!</b>\n\n"
            "Настройте прокси для входа в аккаунт.\n\n"
            "<code>/set_proxy http://user:pass@host:port</code>",
            parse_mode="HTML"
        )
        return

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)

        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        # Проверяем наличие пароля
        if not mafile.password or not mafile.password.strip():
            await callback_feedback(callback)
            await callback.message.edit_text(
                f"⚠️ <b>Пароль отсутствует!</b>\n\n"
                f"Аккаунт: <code>{escape(str(mafile.account_name))}</code>\n\n"
                f"Для входа необходимо знать пароль.\n"
                f"Выберите действие:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="✏️ Ввести пароль",
                            callback_data=f"enter_password_{mafile.id}"
                        )],
                        [InlineKeyboardButton(
                            text="🔄 Восстановить сессию (по refresh token)",
                            callback_data=f"try_refresh_{mafile.id}"
                        )],
                        [InlineKeyboardButton(
                            text="◀️ Назад",
                            callback_data=f"account_detail_{mafile.id}"
                        )]
                    ]
                )
            )
            return

        # Пароль есть - выполняем ПОЛНЫЙ вход
        await callback_feedback(callback, f"Вхожу в {mafile.account_name}...")
        await perform_login_full(
            callback.message,
            mafile,
            session,
            telegram_id=telegram_id,
            state=state,
        )


async def perform_login_full(
    message: Message,
    mafile: Mafile,
    db_session: AsyncSession,
    *,
    telegram_id: int,
    state: FSMContext,
):
    """Полный вход по логину:паролю (без попытки refresh_token).

    ``telegram_id`` is passed explicitly: for button-started logins ``message``
    is the bot's own message, so ``message.from_user`` is the bot, not the user.
    A pending email/mobile challenge is published to the typed FSM and the
    pending-challenge registry; the live client never enters FSM storage.
    """
    status_msg = await message.answer(
        f"🔄 <b>Выполняю вход в {escape(str(mafile.account_name))}...</b>",
        parse_mode="HTML"
    )

    try:
        client = await SteamSessionManager.create_steam_client_from_db(mafile, db_session)
    except ProxyRequiredError:
        # The proxy disappeared after the caller's pre-check; no Steam request
        # was sent. Report it instead of leaving "Выполняю вход..." hanging.
        await status_msg.edit_text(
            "❌ Для входа в Steam сначала настройте общий прокси или "
            "уникальный прокси аккаунта."
        )
        return
    client_owned_by_handler = True
    challenge_id: str | None = None

    async def discard_published_challenge() -> None:
        if challenge_id is not None:
            await clear_owned_pending_challenge(
                challenge_id,
                state,
                user_id=telegram_id,
            )

    try:
        code_type = await client.login()

        if code_type == LoginConfirmType.none:
            # Вход без Steam Guard
            await client.confirm_login()

            # 🔥 Получаем страну сразу после входа
            try:
                country = await client.get_account_country()
                if country:
                    mafile.country = country
                    for curr_id, info in STEAM_CURRENCIES.items():
                        if info['country'] == country:
                            mafile.currency_id = curr_id
                            break
                    await db_session.flush()
                    logger.info(f"🌍 [{mafile.account_name}] Country detected: {country}")
            except Exception as e:
                logger.warning("Could not detect country: %s", type(e).__name__)

            await SteamSessionManager.save_steam_session_to_db(db_session, mafile.id, client)

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="🔐 Подключить Steam Guard",
                        callback_data=f"setup_steamguard_{mafile.id}"
                    )],
                    [InlineKeyboardButton(
                        text="👤 Позже",
                        callback_data=f"account_detail_{mafile.id}"
                    )]
                ]
            )

            await status_msg.edit_text(
                f"✅ <b>Успешный вход!</b>\n"
                f"SteamID: <code>{client.steamid}</code>\n\n"
                f"ℹ️ <b>На аккаунте НЕТ Steam Guard</b>\n\n"
                f"Рекомендуется подключить Steam Guard для безопасности.\n"
                f"Бот может сам создать и настроить Steam Guard!",
                parse_mode="HTML",
                reply_markup=keyboard
            )
            return

        if code_type == LoginConfirmType.mobile and mafile.shared_secret:
            # Есть shared_secret — генерируем код сами
            client.load_mobile({
                'shared_secret': decrypt_value(mafile.shared_secret),
                'device_id': decrypt_value(mafile.device_id) or generate_device_id(),
                'identity_secret': decrypt_value(mafile.identity_secret),
                'serial_number': decrypt_value(mafile.serial_number),
                'revocation_code': decrypt_value(mafile.revocation_code),
                'uri': decrypt_value(mafile.uri),
                'token_gid': decrypt_value(mafile.token_gid),
                'secret_1': decrypt_value(mafile.secret_1)
            })
            await client.align_time()
            code = client.generate_steam_guard_code()

            await status_msg.edit_text(
                f"📱 <b>Вхожу с использованием Steam Guard...</b>\n\n"
                f"Сгенерирован код: <code>{code}</code>",
                parse_mode="HTML"
            )

            try:
                await client.confirm_login(code)

                # 🔥 Получаем страну сразу после входа
                try:
                    country = await client.get_account_country()
                    if country:
                        mafile.country = country
                        for curr_id, info in STEAM_CURRENCIES.items():
                            if info['country'] == country:
                                mafile.currency_id = curr_id
                                break
                        await db_session.flush()
                        logger.info(f"🌍 [{mafile.account_name}] Country detected: {country}")
                except Exception as e:
                    logger.warning("Could not detect country: %s", type(e).__name__)

                await SteamSessionManager.save_steam_session_to_db(db_session, mafile.id, client)

                await status_msg.edit_text(
                    f"✅ <b>Успешный вход!</b>\n"
                    f"SteamID: <code>{client.steamid}</code>\n\n"
                    f"🔐 Steam Guard активирован!\n"
                    f"Используйте <code>/gc {escape(str(mafile.account_name))}</code> для получения кодов.",
                    parse_mode="HTML"
                )
            except InvalidSteamGuardCode:
                await status_msg.edit_text(
                    f"❌ <b>Ошибка входа!</b>\n\n"
                    f"Сгенерированный код <code>{code}</code> не подошел.\n"
                    f"Возможна рассинхронизация времени.\n\n"
                    f"Попробуйте:\n"
                    f"• Синхронизировать время на ПК\n"
                    f"• Перезапустить SDA (если используете)",
                    parse_mode="HTML"
                )
            return

        if code_type == LoginConfirmType.email:
            kind = "email"
            prompt = (
                "📧 <b>Требуется код с EMAIL</b>\n\n"
                "Steam отправил код подтверждения на вашу почту.\n"
                "ℹ️ <i>На аккаунте НЕТ мобильного Steam Guard</i>\n\n"
                "<b>Введите код из письма:</b>\n\n"
                "💡 <i>После входа вы сможете подключить Steam Guard через бота!</i>"
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=f"account_detail_{mafile.id}"
                    )]
                ]
            )
        elif code_type == LoginConfirmType.mobile:
            # Нет shared_secret — ждём код из приложения или импорт maFile
            kind = "mobile"
            prompt = (
                "⚠️ <b>На аккаунте уже подключен Steam Guard!</b>\n\n"
                "У бота нет ключей для генерации кода.\n\n"
                "<b>Отправьте код из приложения Steam Guard / SDA</b> "
                "или импортируйте maFile, чтобы бот генерировал коды сам."
            )
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="📤 Импортировать mafile",
                        callback_data=f"import_mafile_for_{mafile.id}"
                    )],
                    [InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=f"account_detail_{mafile.id}"
                    )]
                ]
            )
        else:
            raise RuntimeError("Unsupported Steam login confirmation type")

        # publish_pending_challenge owns the client from here on: it closes it
        # on any failure, including cancellation while queued for the lock.
        client_owned_by_handler = False
        challenge_id = await publish_pending_challenge(
            user_id=telegram_id,
            mafile_id=mafile.id,
            kind=kind,
            client=client,
            state=state,
            fsm_state=SteamChallengeFlow.waiting_code,
        )
        await status_msg.edit_text(prompt, parse_mode="HTML", reply_markup=keyboard)

    except (TelegramAPIError, asyncio.CancelledError):
        await discard_published_challenge()
        raise
    except InvalidCredentials:
        await discard_published_challenge()
        await status_msg.edit_text("❌ <b>Неверный логин или пароль</b>", parse_mode="HTML")
    except Exception:
        await discard_published_challenge()
        logger.exception("Steam login failed for user_id=%s", telegram_id)
        await status_msg.edit_text(
            "❌ Не удалось выполнить вход в Steam. Проверьте прокси и попробуйте позже."
        )
    finally:
        if client_owned_by_handler:
            try:
                await close_owned_resource(client)
            except Exception:
                logger.warning("Failed to close Steam login client for user_id=%s", telegram_id)


@router.callback_query(F.data.startswith("enter_password_"))
@validated_callback_data(r"enter_password_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_enter_password(callback: CallbackQuery, state: FSMContext):
    """Запрос пароля для входа"""
    mafile_id = int(callback.data.split("_")[2])

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)

        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        await callback_feedback(callback)
        try:
            await clear_user_workflows(callback.from_user.id, state)
            await state.set_state(LoginPasswordFlow.waiting_password)
            await state.set_data({"mafile_id": mafile.id})
            await callback.message.edit_text(
                f"🔑 <b>Введите пароль для {escape(str(mafile.account_name))}:</b>\n\n"
                f"Пароль будет сохранен в базе данных.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="❌ Отмена",
                            callback_data=f"account_detail_{mafile.id}"
                        )]
                    ]
                )
            )
        except (Exception, asyncio.CancelledError):
            await clear_user_workflows(callback.from_user.id, state)
            raise


@router.message(
    LoginPasswordFlow.waiting_password,
    F.text,
    ~F.text.startswith("/"),
)
async def receive_login_password(message: Message, state: FSMContext) -> None:
    """Persist a missing Steam password, then start proxy-enforced login."""
    if not await delete_secret_message_or_abort(message):
        return
    if message.chat.type != ChatType.PRIVATE:
        await message.answer(
            "❌ Пароль можно вводить только в личном чате с ботом."
        )
        return
    if message.from_user is None:
        raise RuntimeError("Telegram user is unavailable for this message")

    state_data = await state.get_data()
    mafile_id = state_data.get("mafile_id")
    if (
        type(mafile_id) is not int
        or mafile_id <= 0
        or mafile_id > _MAX_DATABASE_INTEGER
    ):
        await clear_user_workflows(message.from_user.id, state)
        await message.answer("❌ Сессия устарела. Начните вход заново.")
        return

    password = (message.text or "").strip()
    if not password:
        await message.answer("❌ Пароль не может быть пустым. Попробуйте снова.")
        return

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, message.from_user.id)
        if not mafile:
            await clear_user_workflows(message.from_user.id, state)
            await message.answer("❌ Аккаунт не найден")
            return

        mafile.password = encrypt_value(password)
        await session.commit()
        await clear_user_workflows(message.from_user.id, state)
        await message.answer(
            "✅ <b>Пароль сохранен!</b>\n\nПробую войти в аккаунт...",
            parse_mode="HTML",
        )
        await perform_login_full(
            message,
            mafile,
            session,
            telegram_id=message.from_user.id,
            state=state,
        )


@router.message(LoginPasswordFlow.waiting_password, NonCommandInputFilter())
async def reject_login_password_non_text(
    message: Message,
    state: FSMContext,
) -> None:
    """Prevent media from falling through while a password is expected."""
    if not await delete_secret_message_or_abort(message):
        return
    if message.chat.type != ChatType.PRIVATE:
        await message.answer(
            "❌ Пароль можно вводить только в личном чате с ботом."
        )
        return
    await message.answer("❌ Отправьте пароль текстом или используйте /cancel.")


@router.callback_query(F.data.startswith("try_refresh_"))
@validated_callback_data(r"try_refresh_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_try_refresh(callback: CallbackQuery):
    """Попытка входа через refresh token без пароля"""
    mafile_id = int(callback.data.split("_")[2])

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)

        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        if not mafile.refresh_token:
            await callback_feedback(callback, "Refresh token отсутствует", show_alert=True)
            await callback.message.edit_text(
                f"❌ <b>Refresh token отсутствует!</b>\n\n"
                f"Невозможно восстановить сессию.\n"
                f"Необходимо ввести пароль.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="✏️ Ввести пароль",
                            callback_data=f"enter_password_{mafile.id}"
                        )],
                        [InlineKeyboardButton(
                            text="◀️ Назад",
                            callback_data=f"account_detail_{mafile.id}"
                        )]
                    ]
                )
            )
            return

        await callback_feedback(callback, "Пробую восстановить сессию...")

        status_msg = await callback.message.edit_text(
            f"🔄 <b>Пробую восстановить сессию для {escape(str(mafile.account_name))}...</b>",
            parse_mode="HTML"
        )

        # 🔥 Создаём клиента правильно — через SteamSessionManager
        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            if client.is_token_expired(client.refresh_token):
                await status_msg.edit_text(
                    f"❌ <b>Refresh token истек!</b>\n\n"
                    f"Необходимо ввести пароль для входа.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="✏️ Ввести пароль",
                                callback_data=f"enter_password_{mafile.id}"
                            )],
                            [InlineKeyboardButton(
                                text="◀️ Назад",
                                callback_data=f"account_detail_{mafile.id}"
                            )]
                        ]
                    )
                )
                return

            await client.refresh_access_token()
            await SteamSessionManager.save_steam_session_to_db(session, mafile.id, client)

            has_password = bool(mafile.password and mafile.password.strip())

            if has_password:
                await status_msg.edit_text(
                    f"✅ <b>Сессия восстановлена!</b>\n"
                    f"SteamID: <code>{client.steamid}</code>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="👤 К аккаунту",
                                callback_data=f"account_detail_{mafile.id}"
                            )]
                        ]
                    )
                )
            else:
                await status_msg.edit_text(
                    f"✅ <b>Сессия восстановлена!</b>\n"
                    f"SteamID: <code>{client.steamid}</code>\n\n"
                    f"⚠️ <b>Внимание:</b> Пароль все еще отсутствует.\n"
                    f"Рекомендуется добавить пароль для полного доступа.",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="✏️ Добавить пароль",
                                callback_data=f"enter_password_{mafile.id}"
                            )],
                            [InlineKeyboardButton(
                                text="👤 К аккаунту",
                                callback_data=f"account_detail_{mafile.id}"
                            )]
                        ]
                    )
                )

        except Exception as e:
            await status_msg.edit_text(
                f"❌ <b>Не удалось восстановить сессию:</b>\n"
                f"<code>{escape(str(e))}</code>\n\n"
                f"Необходимо ввести пароль.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="✏️ Ввести пароль",
                            callback_data=f"enter_password_{mafile.id}"
                        )],
                        [InlineKeyboardButton(
                            text="◀️ Назад",
                            callback_data=f"account_detail_{mafile.id}"
                        )]
                    ]
                )
            )
        finally:
            await client.close()
