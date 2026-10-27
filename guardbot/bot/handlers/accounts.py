"""Account slash commands and the guard-code refresh button.

Every other account feature lives in a dedicated module (account_list,
account_settings, account_proxy, account_login, steam_challenge, ...). The
re-exports at the end keep their historical names importable from here for
guardbot.compat (the ``tgbot`` namespace) and older imports.
"""

from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from datetime import datetime
from guardbot.bot.callback_validation import validated_callback_data
from guardbot.bot.pending_challenges import (
    current_pending_challenge_token,
)
from guardbot.bot.runtime import (
    callback_feedback,
    clear_user_workflows,
    require_proxy,
)
from guardbot.bot.states import (
    AccountAddFlow,
    BulkImportFlow,
    NoteFlow,
    ProxyFlow,
)
from guardbot.config import logger
from guardbot.database import AsyncSessionLocal, Mafile, User
from guardbot.security import decrypt_value
from guardbot.services.ownership import get_owned_mafile
from guardbot.steam.client import AsyncSteamMobile, generate_device_id
from sqlalchemy import select
import time
from guardbot.bot.handlers.account_login import perform_login_full
from guardbot.bot.handlers.guard_codes import generate_and_send_code

router = Router(name=__name__)


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


# Historical names of handlers that moved to dedicated modules.
# isort: off
from guardbot.bot.handlers.account_add import (  # noqa: E402,F401
    callback_add_account,
    callback_cancel_state_to_accounts,
    cmd_add_steam,
    receive_account_credentials,
    _start_account_add,
)
from guardbot.bot.handlers.account_list import (  # noqa: E402,F401
    callback_accounts_page,
    callback_my_accounts,
    callback_page_info,
    show_accounts_page,
)
from guardbot.bot.handlers.account_login import (  # noqa: E402,F401
    callback_action_login,
    callback_enter_password,
    callback_try_refresh,
    receive_login_password,
    reject_login_password_non_text,
)
from guardbot.bot.handlers.account_mafile import (  # noqa: E402,F401
    _ACCOUNT_MAFILE_ENCRYPTED_FIELDS,
    _bounded_mafile_string,
    callback_import_mafile_for,
    _MAX_ACCOUNT_MAFILE_BYTES,
    _MAX_MAFILE_VALUE_LENGTH,
    receive_account_mafile,
    _validated_account_mafile,
)
from guardbot.bot.handlers.account_maintenance import (  # noqa: E402,F401
    callback_action_check_token,
    callback_confirm_delete,
    callback_delete_account,
)
from guardbot.bot.handlers.account_proxy import (  # noqa: E402,F401
    callback_cancel_state_to_settings,
    callback_change_unique_proxy,
    callback_remove_account_proxy,
    callback_set_unique_proxy,
    callback_test_account_proxy,
    receive_unique_proxy,
    _start_unique_proxy_prompt,
)
from guardbot.bot.handlers.account_settings import (  # noqa: E402,F401
    callback_account_detail,
    callback_account_settings,
    callback_change_note,
    callback_toggle_unique_proxy,
    process_account_note,
    reject_note_non_text,
    update_account_settings_message,
)
from guardbot.bot.handlers.callback_ids import (  # noqa: E402,F401
    _MAX_DATABASE_INTEGER,
    _parse_nonnegative_callback_id,
    _parse_positive_callback_id,
)
from guardbot.bot.handlers.guard_codes import (  # noqa: E402,F401
    callback_action_get_code,
)
from guardbot.bot.handlers.menus import (  # noqa: E402,F401
    callback_info,
    callback_instruction,
    callback_mafile_instruction,
    callback_settings,
    update_settings_message,
)
from guardbot.bot.handlers.steam_challenge import (  # noqa: E402,F401
    callback_manual_code,
    receive_manual_steam_code,
    reject_challenge_document,
    _STEAM_CHALLENGE_KINDS,
)
# isort: on
