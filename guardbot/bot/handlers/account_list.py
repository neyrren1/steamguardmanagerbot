"""Paginated list of the user's Steam accounts."""

from html import escape
from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from guardbot.bot.runtime import callback_feedback
from guardbot.database import AsyncSessionLocal, Mafile
from guardbot.services.ownership import get_owned_group
from sqlalchemy import select
from typing import Optional
from guardbot.bot.handlers.callback_ids import _parse_nonnegative_callback_id

router = Router(name=__name__)


@router.callback_query(F.data == "my_accounts")
async def callback_my_accounts(callback: CallbackQuery):
    """Список аккаунтов с пагинацией"""
    await callback_feedback(callback)
    await show_accounts_page(callback, page=0)


@router.callback_query(F.data.startswith("accounts_page_"))
async def callback_accounts_page(callback: CallbackQuery):
    """Переключение страниц аккаунтов"""
    page = _parse_nonnegative_callback_id(callback.data, "accounts_page_")
    if page is None:
        await callback_feedback(callback, "❌ Некорректное действие")
        return
    await callback_feedback(callback)
    await show_accounts_page(callback, page)


async def show_accounts_page(callback: CallbackQuery, page: int, group_id: Optional[int] = None):
    """Отображение страницы с аккаунтами"""
    telegram_id = callback.from_user.id
    ACCOUNTS_PER_PAGE = 5

    async with AsyncSessionLocal() as session:
        # Строим запрос
        stmt = select(Mafile).where(Mafile.telegram_id == telegram_id)

        if group_id == 0:
            stmt = stmt.where(Mafile.group_id == None)
        elif group_id is not None:
            stmt = stmt.where(Mafile.group_id == group_id)

        stmt = stmt.order_by(
            Mafile.is_pinned.desc(),
            Mafile.sort_order.asc(),
            Mafile.id.asc()
        )

        result = await session.execute(stmt)
        all_accounts = result.scalars().all()

        # Получаем название группы
        group_name = "Все аккаунты"
        if group_id == 0:
            group_name = "Без группы"
        elif group_id is not None:
            grp = await get_owned_group(session, group_id, telegram_id)
            if grp:
                group_name = f"Группа: {escape(grp.name)}"

        if not all_accounts:
            text = f"📂 {group_name}\n\n❌ Нет аккаунтов в этой группе"
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="📂 Все группы", callback_data="groups_menu")],
                    [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="add_account")],
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_start")]
                ]
            )
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
            return

        total_accounts = len(all_accounts)
        total_pages = (total_accounts + ACCOUNTS_PER_PAGE - 1) // ACCOUNTS_PER_PAGE

        if page < 0:
            page = 0
        if page >= total_pages:
            page = total_pages - 1

        start_idx = page * ACCOUNTS_PER_PAGE
        end_idx = start_idx + ACCOUNTS_PER_PAGE
        page_accounts = all_accounts[start_idx:end_idx]

        accounts_with_2fa = len([a for a in all_accounts if a.shared_secret])
        accounts_with_full_login = len([a for a in all_accounts if (
            a.access_token and a.refresh_token and a.password and a.password.strip()
        )])

        text = (
            f"📋 <b>ВАШИ STEAM АККАУНТЫ</b>\n"
            f"📂 {group_name}\n"
            f"Всего: {total_accounts} | 🔐 2FA: {accounts_with_2fa} | ✅ Полный вход: {accounts_with_full_login}\n"
            f"Страница {page + 1} из {total_pages}\n\n"
            f"<i>Нажмите на аккаунт для управления:</i>\n"
        )

        keyboard_buttons = []

        for idx, acc in enumerate(page_accounts, start=start_idx + 1):
            has_full_login = bool(acc.access_token and acc.refresh_token and acc.password and acc.password.strip())
            token_status = "✅" if has_full_login else "⚠️"
            guard_status = "🔐" if acc.shared_secret else "❌"
            pin_status = "📌" if acc.is_pinned else ""

            if acc.steamid:
                steamid_str = str(acc.steamid)
                steamid_short = f"{steamid_str[:4]}...{steamid_str[-4:]}" if len(steamid_str) > 8 else steamid_str
            else:
                steamid_short = "нет"

            button_text = f"{idx}. {pin_status}{token_status}{guard_status} {acc.account_name} [{steamid_short}]"
            if len(button_text) > 40:
                button_text = f"{idx}. {pin_status}{token_status}{guard_status} {acc.account_name[:25]}..."

            keyboard_buttons.append([
                InlineKeyboardButton(
                    text=button_text,
                    callback_data=f"account_detail_{acc.id}"
                )
            ])

        nav_buttons = []

        if page > 0:
            nav_buttons.append(InlineKeyboardButton(
                text="◀️ Назад",
                callback_data=f"accounts_page_{page - 1}"
            ))

        nav_buttons.append(InlineKeyboardButton(
            text=f"📄 {page + 1}/{total_pages}",
            callback_data="page_info"
        ))

        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton(
                text="Вперед ▶️",
                callback_data=f"accounts_page_{page + 1}"
            ))

        keyboard_buttons.append(nav_buttons)

        keyboard_buttons.append([
            InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="add_account")
        ])
        keyboard_buttons.append([
            InlineKeyboardButton(text="📂 Группы", callback_data="groups_menu")
        ])
        keyboard_buttons.append([
            InlineKeyboardButton(text="📦 Массовый импорт", callback_data="bulk_import_menu"),
            InlineKeyboardButton(text="📁 Импорт mafile", callback_data="mafile_instruction")
        ])
        keyboard_buttons.append([
            InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_start")
        ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data == "page_info")
async def callback_page_info(callback: CallbackQuery):
    """Заглушка для кнопки с номером страницы"""
    await callback_feedback(callback, "Это текущая страница", show_alert=False)
