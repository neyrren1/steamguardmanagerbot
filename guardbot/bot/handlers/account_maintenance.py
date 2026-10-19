"""Session-token check and account deletion with confirmation."""

from html import escape
from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from datetime import datetime
from guardbot.bot.callback_validation import validated_callback_data
from guardbot.bot.runtime import callback_feedback
from guardbot.database import AsyncSessionLocal
from guardbot.security import decrypt_value
from guardbot.services.ownership import get_owned_mafile
from guardbot.steam.client import AsyncSteamMobile

router = Router(name=__name__)


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
