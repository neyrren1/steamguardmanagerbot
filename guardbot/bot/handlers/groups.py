"""Extracted from the legacy bot module without behavior changes."""

from aiogram import F
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from guardbot.bot.handlers.accounts import callback_account_settings, callback_settings, show_accounts_page, update_account_settings_message
from guardbot.bot.handlers.commands import build_mafile_dict
from guardbot.bot.runtime import dp, user_states
from guardbot.database import AccountGroup, AsyncSessionLocal, Mafile, User
from sqlalchemy import func, select
import io
import json
import re
import zipfile

# ==================== ЗАКРЕПЛЕНИЕ АККАУНТА ====================
@dp.callback_query(F.data.startswith("toggle_pin_"))
async def callback_toggle_pin(callback: CallbackQuery):
    """Закрепление/открепление аккаунта в списке"""
    mafile_id = int(callback.data.split("_")[2])

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        mafile.is_pinned = not mafile.is_pinned
        await session.commit()

        if mafile.is_pinned:
            await callback.answer("📌 Аккаунт закреплен в списке", show_alert=True)
        else:
            await callback.answer("📍 Аккаунт откреплен", show_alert=True)

        await update_account_settings_message(callback.message, mafile, session)


# ==================== ГРУППЫ ====================
@dp.callback_query(F.data == "groups_menu")
async def callback_groups_menu(callback: CallbackQuery):
    """Меню групп"""
    telegram_id = callback.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(AccountGroup).where(AccountGroup.telegram_id == telegram_id).order_by(AccountGroup.sort_order)
        result = await session.execute(stmt)
        groups = result.scalars().all()

        stmt = select(func.count(Mafile.id)).where(Mafile.telegram_id == telegram_id, Mafile.group_id == None)
        result = await session.execute(stmt)
        no_group_count = result.scalar() or 0

    keyboard_buttons = []

    if no_group_count > 0:
        keyboard_buttons.append([
            InlineKeyboardButton(
                text=f"📂 Без группы ({no_group_count})",
                callback_data="list_group_none_0"
            )
        ])

    for grp in groups:
        stmt = select(func.count(Mafile.id)).where(Mafile.group_id == grp.id)
        result = await session.execute(stmt)
        count = result.scalar() or 0

        keyboard_buttons.append([
            InlineKeyboardButton(
                text=f"📁 {grp.name} ({count})",
                callback_data=f"view_group_{grp.id}_0"
            )
        ])

    keyboard_buttons.append([
        InlineKeyboardButton(text="➕ Создать группу", callback_data="create_group")
    ])
    keyboard_buttons.append([
        InlineKeyboardButton(text="📋 Все аккаунты", callback_data="list_group_all_0")
    ])
    keyboard_buttons.append([
        InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_start")
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    await callback.message.edit_text(
        "📂 <b>ГРУППЫ АККАУНТОВ</b>\n\n"
        f"Всего групп: {len(groups)}\n"
        f"Аккаунтов без группы: {no_group_count}\n\n"
        "Выберите группу для просмотра:",
        parse_mode="HTML",
        reply_markup=keyboard
    )
    await callback.answer()


@dp.callback_query(F.data == "create_group")
async def callback_create_group(callback: CallbackQuery):
    """Создание новой группы"""
    user_states[callback.from_user.id] = {
        "state": "waiting_group_name"
    }
    await callback.message.edit_text(
        "📁 <b>СОЗДАНИЕ ГРУППЫ</b>\n\n"
        "Отправьте название новой группы:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="❌ Отмена", callback_data="groups_menu")
            ]]
        )
    )
    await callback.answer()



@dp.callback_query(F.data.startswith("view_group_"))
async def callback_view_group(callback: CallbackQuery):
    """Просмотр группы с возможностью действий"""
    parts = callback.data.split("_")
    group_id = int(parts[2])
    page = int(parts[3])

    async with AsyncSessionLocal() as session:
        grp = await session.get(AccountGroup, group_id)
        if not grp:
            await callback.answer("Группа не найдена", show_alert=True)
            return

        stmt = select(func.count(Mafile.id)).where(Mafile.group_id == group_id)
        result = await session.execute(stmt)
        count = result.scalar() or 0

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"📋 Показать аккаунты ({count})",
                callback_data=f"list_group_id_{group_id}_0"
            )],
            [InlineKeyboardButton(
                text="✏️ Переименовать",
                callback_data=f"rename_group_{group_id}"
            )],
            [InlineKeyboardButton(
                text="📤 Экспортировать",
                callback_data=f"export_group_{group_id}"
            )],
            [InlineKeyboardButton(
                text="🗑️ Удалить группу",
                callback_data=f"delete_group_{group_id}"
            )],
            [InlineKeyboardButton(
                text="◀️ К списку групп",
                callback_data="groups_menu"
            )]
        ]
    )

    await callback.message.edit_text(
        f"📁 <b>Группа: {grp.name}</b>\n\n"
        f"Аккаунтов в группе: {count}\n\n"
        "Выберите действие:",
        parse_mode="HTML",
        reply_markup=keyboard
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("export_group_"))
async def callback_export_group(callback: CallbackQuery):
    """Экспорт группы через кнопку"""
    group_id = int(callback.data.split("_")[2])
    telegram_id = callback.from_user.id

    async with AsyncSessionLocal() as session:
        grp = await session.get(AccountGroup, group_id)
        if not grp:
            await callback.answer("Группа не найдена", show_alert=True)
            return

        # Получаем аккаунты группы
        stmt = select(Mafile).where(Mafile.group_id == group_id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await callback.answer("В группе нет аккаунтов", show_alert=True)
            return

        await callback.answer(f"📤 Экспортирую {len(mafiles)} аккаунтов...")

        status_msg = await callback.message.edit_text(
            f"🔄 <b>Создаю архив группы \"{grp.name}\"...</b>",
            parse_mode="HTML"
        )

        try:
            zip_buffer = io.BytesIO()

            with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                for mafile in mafiles:
                    mafile_data = build_mafile_dict(mafile)
                    if mafile_data:
                        json_str = json.dumps(mafile_data, indent=2, ensure_ascii=False)
                        filename = f"{mafile.account_name}.mafile"
                        zip_file.writestr(filename, json_str)

            zip_buffer.seek(0)



            safe_name = re.sub(r'[^\w\s-]', '', grp.name).strip()[:50]
            if not safe_name:
                safe_name = "group"

            # Удаляем статусное сообщение
            await status_msg.delete()

            await callback.message.answer_document(
                BufferedInputFile(
                    zip_buffer.getvalue(),
                    filename=f"{safe_name}_mafiles.zip"
                ),
                caption=f"📦 Группа \"{grp.name}\": {len(mafiles)} аккаунтов"
            )

            # Показываем снова меню группы
            await callback.message.answer(
                f"📁 <b>Группа: {grp.name}</b>\n\n"
                f"✅ Архив экспортирован!\n\n"
                f"Выберите действие:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text=f"📋 Показать аккаунты ({len(mafiles)})",
                            callback_data=f"list_group_id_{group_id}_0"
                        )],
                        [InlineKeyboardButton(
                            text="✏️ Переименовать",
                            callback_data=f"rename_group_{group_id}"
                        )],
                        [InlineKeyboardButton(
                            text="📤 Экспортировать",
                            callback_data=f"export_group_{group_id}"
                        )],
                        [InlineKeyboardButton(
                            text="🗑️ Удалить группу",
                            callback_data=f"delete_group_{group_id}"
                        )],
                        [InlineKeyboardButton(
                            text="◀️ К списку групп",
                            callback_data="groups_menu"
                        )]
                    ]
                )
            )

        except Exception as e:
            await status_msg.edit_text(
                f"❌ Ошибка при создании архива: {str(e)[:200]}",
                parse_mode="HTML"
            )

# Сначала более специфичный
@dp.callback_query(F.data.startswith("list_group_id_"))
async def callback_list_group_id(callback: CallbackQuery):
    """Показать аккаунты конкретной группы"""
    parts = callback.data.split("_")
    group_id = int(parts[3])
    page = int(parts[4])
    await show_accounts_page(callback, page, group_id)

# Потом общий list_group_
@dp.callback_query(F.data.startswith("list_group_"))
async def callback_list_group_filter(callback: CallbackQuery):
    """Фильтр аккаунтов по группе"""
    parts = callback.data.split("_")
    group_type = parts[2]

    if group_type == "all":
        await show_accounts_page(callback, 0, None)
    elif group_type == "none":
        await show_accounts_page(callback, 0, 0)
    await callback.answer()

@dp.callback_query(F.data.startswith("rename_group_"))
async def callback_rename_group(callback: CallbackQuery):
    """Переименование группы"""
    group_id = int(callback.data.split("_")[2])
    user_states[callback.from_user.id] = {
        "state": "waiting_group_rename",
        "group_id": group_id
    }
    await callback.message.edit_text(
        "✏️ <b>ПЕРЕИМЕНОВАНИЕ ГРУППЫ</b>\n\n"
        "Отправьте новое название:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(text="❌ Отмена", callback_data="groups_menu")
            ]]
        )
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("delete_group_"))
async def callback_delete_group(callback: CallbackQuery):
    """Удаление группы"""
    group_id = int(callback.data.split("_")[2])

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Да, удалить",
                    callback_data=f"confirm_delete_group_{group_id}"
                ),
                InlineKeyboardButton(
                    text="❌ Отмена",
                    callback_data="groups_menu"
                )
            ]
        ]
    )

    await callback.message.edit_text(
        "⚠️ <b>УДАЛЕНИЕ ГРУППЫ</b>\n\n"
        "Вы уверены? Аккаунты не будут удалены, они останутся без группы.",
        parse_mode="HTML",
        reply_markup=keyboard
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("confirm_delete_group_"))
async def callback_confirm_delete_group(callback: CallbackQuery):
    """Подтверждение удаления группы"""
    group_id = int(callback.data.split("_")[3])

    async with AsyncSessionLocal() as session:
        grp = await session.get(AccountGroup, group_id)
        if not grp:
            await callback.answer("Группа не найдена", show_alert=True)
            return

        group_name = grp.name
        await session.delete(grp)
        await session.commit()

    await callback.answer(f"✅ Группа '{group_name}' удалена", show_alert=True)
    await callback_groups_menu(callback)


# ==================== СМЕНА ГРУППЫ АККАУНТА ====================

@dp.callback_query(F.data.startswith("change_group_"))
async def callback_change_group(callback: CallbackQuery):
    """Смена группы аккаунта"""
    mafile_id = int(callback.data.split("_")[2])

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        # 🔥 Получаем имя группы пока сессия жива
        current_group_name = "Без группы"
        if mafile.group_id:
            grp = await session.get(AccountGroup, mafile.group_id)
            if grp:
                current_group_name = grp.name

        stmt = select(AccountGroup).where(AccountGroup.telegram_id == mafile.telegram_id).order_by(AccountGroup.sort_order)
        result = await session.execute(stmt)
        groups = result.scalars().all()

    keyboard_buttons = []

    if mafile.group_id is not None:
        keyboard_buttons.append([
            InlineKeyboardButton(
                text="📂 Без группы",
                callback_data=f"set_group_{mafile_id}_0"
            )
        ])

    for grp in groups:
        if grp.id == mafile.group_id:
            continue
        keyboard_buttons.append([
            InlineKeyboardButton(
                text=grp.name,
                callback_data=f"set_group_{mafile_id}_{grp.id}"
            )
        ])

    keyboard_buttons.append([
        InlineKeyboardButton(text="◀️ Назад", callback_data=f"account_settings_{mafile_id}")
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    await callback.message.edit_text(
        f"📂 <b>СМЕНА ГРУППЫ</b>\n\n"
        f"Аккаунт: <code>{mafile.account_name}</code>\n"
        f"Текущая группа: <b>{current_group_name}</b>\n\n"
        f"Выберите новую группу:",
        parse_mode="HTML",
        reply_markup=keyboard
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("set_group_"))
async def callback_set_group(callback: CallbackQuery):
    """Установка группы аккаунту"""
    parts = callback.data.split("_")
    mafile_id = int(parts[2])
    group_id = int(parts[3])

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        mafile.group_id = group_id if group_id > 0 else None
        await session.commit()

        group_name = "Без группы"
        if group_id > 0:
            grp = await session.get(AccountGroup, group_id)
            if grp:
                group_name = grp.name

    await callback.answer(f"✅ Перемещено в: {group_name}", show_alert=True)
    await callback_account_settings(callback)

@dp.callback_query(F.data.startswith("set_timeout_"))
async def callback_set_timeout_value(callback: CallbackQuery):
    """Установка значения таймаута"""
    timeout = int(callback.data.split("_")[2])
    telegram_id = callback.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if not user:
            await callback.answer("❌ Пользователь не найден", show_alert=True)
            return

        user.lock_timeout = timeout if timeout > 0 else None
        await session.commit()

    if timeout > 0:
        await callback.answer(f"✅ Автоблокировка через {timeout} мин", show_alert=True)
    else:
        await callback.answer("✅ Автоблокировка выключена", show_alert=True)

    await callback_settings(callback)
