"""Steam trade-link handler."""

from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from guardbot.bot.callback_validation import validated_callback_data
from guardbot.bot.runtime import callback_feedback
from guardbot.database import AsyncSessionLocal
from guardbot.services.ownership import get_owned_mafile
from guardbot.services.session_manager import SteamSessionManager

router = Router(name=__name__)


@router.callback_query(F.data.startswith("tradelink_"))
@validated_callback_data(r"tradelink_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_tradelink(callback: CallbackQuery):
    """Получение трейд-ссылки"""
    mafile_id = int(callback.data.split("_")[1])

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        if not mafile.access_token:
            await callback_feedback(callback, "Нет активной сессии", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)
        try:
            tradelink = await client.get_tradelink()

            if tradelink:
                await callback.message.edit_text(
                    f"🔗 <b>Трейд-ссылка:</b>\n\n"
                    f"<code>{escape(str(tradelink))}</code>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[
                            InlineKeyboardButton(text="◀️ Назад", callback_data=f"trades_menu_{mafile_id}")
                        ]]
                    )
                )
            else:
                await callback_feedback(callback, "Не удалось получить трейд-ссылку", show_alert=True)
        except Exception as e:
            await callback_feedback(callback, f"Ошибка: {str(e)[:50]}", show_alert=True)
        finally:
            await client.close()
