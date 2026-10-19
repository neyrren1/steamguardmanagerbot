"""Steam Guard code generation and the account card's get-code button."""

from html import escape
from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from guardbot.bot.callback_validation import validated_callback_data
from guardbot.bot.runtime import callback_feedback
from guardbot.config import logger
from guardbot.database import AsyncSessionLocal, Mafile, User
from guardbot.security import decrypt_value
from guardbot.services.ownership import get_owned_mafile
from guardbot.steam.client import AsyncSteamMobile, generate_device_id
from sqlalchemy import select
import time

router = Router(name=__name__)


@router.callback_query(F.data.startswith("action_get_code_"))
@validated_callback_data(r"action_get_code_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_action_get_code(callback: CallbackQuery):
    """Получение кода по кнопке из деталей аккаунта"""
    mafile_id = int(callback.data.split("_")[3])

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)

        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        if not mafile.shared_secret:
            await callback_feedback(callback, "Steam Guard не активирован", show_alert=True)
            return

        await callback_feedback(callback, "Генерирую код...")
        await generate_and_send_code(callback.message, mafile)


async def generate_and_send_code(message: Message, mafile: Mafile):
    """Генерация и отправка кода с красивым интерфейсом"""
    status_msg = await message.answer(
        f"🔄 <b>Генерирую код для {escape(str(mafile.account_name))}...</b>",
        parse_mode="HTML"
    )

    # 🔥 Получаем и расшифровываем прокси для клиента
    proxy = None
    async with AsyncSessionLocal() as session:
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

        # 🔥 Пробуем синхронизировать время
        try:
            await client.align_time()
        except Exception as e:
            logger.warning(f"Не удалось синхронизировать время: {e}")

        code = client.generate_steam_guard_code()

        if not code:
            await status_msg.edit_text(
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

        # 🔥 Добавляем информацию о времени
        time_source = "Steam" if client.is_using_steam_time() else "локальное"

        message_text = (
            f"<b>🔐 STEAM GUARD КОД</b>\n\n"
            f"<b>📱 Аккаунт:</b> <code>{escape(str(mafile.account_name))}</code>\n"
            f"<b>🔑 Код:</b> <code>{code}</code>\n\n"
            f"<code>[{progress_bar}]</code> обновится через <b>{remaining_seconds}с</b>\n"
            f"<i>⏰ Время: {time_source}</i>"
        )

        # Создаем клавиатуру с кнопкой обновления
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="🔄 Обновить код",
                    callback_data=f"refresh_code_{mafile.id}"
                )]
            ]
        )

        await status_msg.edit_text(message_text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        await status_msg.edit_text(
            f"❌ Ошибка при генерации кода:\n<code>{escape(str(e))}</code>",
            parse_mode="HTML"
        )
        logger.error(f"Ошибка генерации кода для {mafile.account_name}: {e}")
    finally:
        await client.close()
