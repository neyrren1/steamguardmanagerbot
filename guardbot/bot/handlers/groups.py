"""Extracted from the legacy bot module without behavior changes."""

import asyncio
from contextlib import suppress
from html import escape
import io
import json
import re
import zipfile

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from guardbot.bot.callback_validation import validated_callback_data
from guardbot.bot.handlers.accounts import callback_account_settings, callback_settings, show_accounts_page, update_account_settings_message
from guardbot.bot.handlers.commands import build_mafile_dict
from guardbot.bot.runtime import callback_feedback, clear_user_workflows
from guardbot.bot.states import GroupFlow
from guardbot.database import AccountGroup, AsyncSessionLocal, Mafile, User
from guardbot.services.ownership import get_owned_group, get_owned_mafile
from sqlalchemy import func, select

router = Router(name=__name__)

# ==================== ЗАКРЕПЛЕНИЕ АККАУНТА ====================
@router.callback_query(F.data.startswith("toggle_pin_"))
@validated_callback_data(r"toggle_pin_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_toggle_pin(callback: CallbackQuery):
    """Закрепление/открепление аккаунта в списке"""
    mafile_id = int(callback.data.split("_")[2])

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        mafile.is_pinned = not mafile.is_pinned
        await session.commit()

        if mafile.is_pinned:
            await callback_feedback(callback, "📌 Аккаунт закреплен в списке", show_alert=True)
        else:
            await callback_feedback(callback, "📍 Аккаунт откреплен", show_alert=True)

        await update_account_settings_message(callback.message, mafile, session)


# ==================== ГРУППЫ ====================
MAX_GROUP_NAME_LENGTH = 100


async def _validated_group_name(message: Message) -> str | None:
    group_name = (message.text or "").strip()
    if not group_name:
        await message.answer("❌ Название группы не может быть пустым")
        return None
    if len(group_name) > MAX_GROUP_NAME_LENGTH:
        await message.answer(
            f"❌ Название группы не должно превышать {MAX_GROUP_NAME_LENGTH} символов"
        )
        return None
    return group_name


@router.callback_query(F.data == "groups_menu")
async def callback_groups_menu(callback: CallbackQuery, state: FSMContext):
    """Меню групп"""
    telegram_id = callback.from_user.id
    await clear_user_workflows(telegram_id, state)

    async with AsyncSessionLocal() as session:
        stmt = select(AccountGroup).where(AccountGroup.telegram_id == telegram_id).order_by(AccountGroup.sort_order)
        result = await session.execute(stmt)
        groups = result.scalars().all()

        stmt = select(func.count(Mafile.id)).where(Mafile.telegram_id == telegram_id, Mafile.group_id == None)
        result = await session.execute(stmt)
        no_group_count = result.scalar() or 0

        group_counts = []
        for group in groups:
            stmt = select(func.count(Mafile.id)).where(Mafile.group_id == group.id)
            result = await session.execute(stmt)
            group_counts.append((group, result.scalar() or 0))

    keyboard_buttons = []

    if no_group_count > 0:
        keyboard_buttons.append([
            InlineKeyboardButton(
                text=f"📂 Без группы ({no_group_count})",
                callback_data="list_group_none_0"
            )
        ])

    for grp, count in group_counts:
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
    await callback_feedback(callback)


@router.callback_query(F.data == "create_group")
async def callback_create_group(callback: CallbackQuery, state: FSMContext):
    """Создание новой группы"""
    await clear_user_workflows(callback.from_user.id, state)
    try:
        await state.set_state(GroupFlow.waiting_name)
        await state.set_data({})
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
    except (Exception, asyncio.CancelledError):
        await clear_user_workflows(callback.from_user.id, state)
        raise
    await callback_feedback(callback)


@router.message(GroupFlow.waiting_name, F.text, ~F.text.startswith("/"))
async def process_group_name(message: Message, state: FSMContext):
    """Create a group from a typed FSM prompt."""
    if message.chat.type != ChatType.PRIVATE:
        await message.answer(
            "❌ Название группы можно вводить только в личном чате с ботом."
        )
        return

    group_name = await _validated_group_name(message)
    if group_name is None:
        return

    with suppress(Exception):
        await message.delete()

    async with AsyncSessionLocal() as session:
        stmt = select(func.max(AccountGroup.sort_order)).where(
            AccountGroup.telegram_id == message.from_user.id
        )
        result = await session.execute(stmt)
        max_order = result.scalar() or 0

        group = AccountGroup(
            telegram_id=message.from_user.id,
            name=group_name,
            sort_order=max_order + 1,
        )
        session.add(group)
        await session.commit()

    await clear_user_workflows(message.from_user.id, state)
    await message.answer(
        f"✅ Группа <b>{escape(group_name)}</b> создана!\n"
        "Используйте /groups для просмотра.",
        parse_mode="HTML",
    )



@router.callback_query(F.data.startswith("view_group_"))
async def callback_view_group(callback: CallbackQuery):
    """Просмотр группы с возможностью действий"""
    match = re.fullmatch(
        r"view_group_([1-9][0-9]{0,9})_([0-9]{1,10})",
        callback.data or "",
    )
    if match is None:
        await callback_feedback(callback, "❌ Некорректное действие")
        return
    group_id, page = map(int, match.groups())
    if group_id > 2_147_483_647 or page > 2_147_483_647:
        await callback_feedback(callback, "❌ Некорректное действие")
        return

    async with AsyncSessionLocal() as session:
        grp = await get_owned_group(session, group_id, callback.from_user.id)
        if not grp:
            await callback_feedback(callback, "Группа не найдена", show_alert=True)
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
        f"📁 <b>Группа: {escape(grp.name)}</b>\n\n"
        f"Аккаунтов в группе: {count}\n\n"
        "Выберите действие:",
        parse_mode="HTML",
        reply_markup=keyboard
    )
    await callback_feedback(callback)

@router.callback_query(F.data.startswith("export_group_"))
@validated_callback_data(r"export_group_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_export_group(callback: CallbackQuery):
    """Экспорт группы через кнопку"""
    if (
        not callback.message
        or callback.message.chat.type != ChatType.PRIVATE
    ):
        await callback_feedback(
            callback,
            "❌ Экспорт доступен только в личном чате с ботом.",
            show_alert=True,
        )
        return

    group_id = int(callback.data.split("_")[2])
    telegram_id = callback.from_user.id

    async with AsyncSessionLocal() as session:
        grp = await get_owned_group(session, group_id, callback.from_user.id)
        if not grp:
            await callback_feedback(callback, "Группа не найдена", show_alert=True)
            return

        # Получаем аккаунты группы
        stmt = select(Mafile).where(Mafile.group_id == group_id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        if not mafiles:
            await callback_feedback(callback, "В группе нет аккаунтов", show_alert=True)
            return

        await callback_feedback(callback, f"📤 Экспортирую {len(mafiles)} аккаунтов...")

        status_msg = await callback.message.edit_text(
            f"🔄 <b>Создаю архив группы \"{escape(grp.name)}\"...</b>",
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
                f"📁 <b>Группа: {escape(grp.name)}</b>\n\n"
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
                f"❌ Ошибка при создании архива: {escape(str(e)[:200])}",
                parse_mode="HTML"
            )

# Сначала более специфичный
@router.callback_query(F.data.startswith("list_group_id_"))
@validated_callback_data(
    r"list_group_id_(?P<db_id>[1-9][0-9]{0,9})_"
    r"(?P<int32_page>[0-9]{1,10})"
)
async def callback_list_group_id(callback: CallbackQuery):
    """Показать аккаунты конкретной группы"""
    parts = callback.data.split("_")
    group_id = int(parts[3])
    page = int(parts[4])
    await callback_feedback(callback)
    await show_accounts_page(callback, page, group_id)

# Потом общий list_group_
@router.callback_query(F.data.startswith("list_group_"))
@validated_callback_data(r"list_group_(?:all|none)_0")
async def callback_list_group_filter(callback: CallbackQuery):
    """Фильтр аккаунтов по группе"""
    parts = callback.data.split("_")
    group_type = parts[2]

    if group_type == "all":
        await show_accounts_page(callback, 0, None)
    elif group_type == "none":
        await show_accounts_page(callback, 0, 0)
    await callback_feedback(callback)

@router.callback_query(F.data.startswith("rename_group_"))
@validated_callback_data(r"rename_group_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_rename_group(callback: CallbackQuery, state: FSMContext):
    """Переименование группы"""
    group_id = int(callback.data.split("_")[2])
    await clear_user_workflows(callback.from_user.id, state)
    try:
        await state.set_state(GroupFlow.waiting_rename)
        await state.set_data({"group_id": group_id})
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
    except (Exception, asyncio.CancelledError):
        await clear_user_workflows(callback.from_user.id, state)
        raise
    await callback_feedback(callback)


@router.message(GroupFlow.waiting_rename, F.text, ~F.text.startswith("/"))
async def process_group_rename(message: Message, state: FSMContext):
    """Rename an owned group from a typed FSM prompt."""
    if message.chat.type != ChatType.PRIVATE:
        await message.answer(
            "❌ Новое название группы можно вводить только в личном чате с ботом."
        )
        return

    new_name = await _validated_group_name(message)
    if new_name is None:
        return
    state_data = await state.get_data()
    group_id = state_data.get("group_id")

    with suppress(Exception):
        await message.delete()

    async with AsyncSessionLocal() as session:
        group = await get_owned_group(
            session,
            group_id,
            message.from_user.id,
        )
        if not group:
            await clear_user_workflows(message.from_user.id, state)
            await message.answer("❌ Группа не найдена")
            return

        old_name = group.name
        group.name = new_name
        await session.commit()

    await clear_user_workflows(message.from_user.id, state)
    await message.answer(
        "✅ Группа переименована:\n"
        f"<b>{escape(old_name)}</b> → <b>{escape(new_name)}</b>",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("delete_group_"))
@validated_callback_data(r"delete_group_(?P<db_id>[1-9][0-9]{0,9})")
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
    await callback_feedback(callback)


@router.callback_query(F.data.startswith("confirm_delete_group_"))
@validated_callback_data(r"confirm_delete_group_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_confirm_delete_group(callback: CallbackQuery, state: FSMContext):
    """Подтверждение удаления группы"""
    group_id = int(callback.data.split("_")[3])

    async with AsyncSessionLocal() as session:
        grp = await get_owned_group(session, group_id, callback.from_user.id)
        if not grp:
            await callback_feedback(callback, "Группа не найдена", show_alert=True)
            return

        group_name = grp.name
        await session.delete(grp)
        await session.commit()

    await callback_feedback(callback, f"✅ Группа '{group_name}' удалена", show_alert=True)
    await callback_groups_menu(callback, state)


# ==================== СМЕНА ГРУППЫ АККАУНТА ====================

@router.callback_query(F.data.startswith("change_group_"))
@validated_callback_data(r"change_group_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_change_group(callback: CallbackQuery):
    """Смена группы аккаунта"""
    mafile_id = int(callback.data.split("_")[2])

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        # 🔥 Получаем имя группы пока сессия жива
        current_group_name = "Без группы"
        if mafile.group_id:
            grp = await get_owned_group(session, mafile.group_id, callback.from_user.id)
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
        f"Аккаунт: <code>{escape(mafile.account_name)}</code>\n"
        f"Текущая группа: <b>{escape(current_group_name)}</b>\n\n"
        f"Выберите новую группу:",
        parse_mode="HTML",
        reply_markup=keyboard
    )
    await callback_feedback(callback)

@router.callback_query(F.data.startswith("set_group_"))
@validated_callback_data(
    r"set_group_(?P<db_id>[1-9][0-9]{0,9})_"
    r"(?P<int32_group>[0-9]{1,10})"
)
async def callback_set_group(callback: CallbackQuery):
    """Установка группы аккаунту"""
    parts = callback.data.split("_")
    mafile_id = int(parts[2])
    group_id = int(parts[3])

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        group_name = "Без группы"
        if group_id > 0:
            grp = await get_owned_group(session, group_id, callback.from_user.id)
            if not grp:
                await callback_feedback(callback, "Группа не найдена", show_alert=True)
                return
            group_name = grp.name

        mafile.group_id = group_id if group_id > 0 else None
        await session.commit()

    await callback_feedback(callback, f"✅ Перемещено в: {group_name}", show_alert=True)
    callback.data = f"account_settings_{mafile_id}"
    await callback_account_settings(callback)

@router.callback_query(F.data.startswith("set_timeout_"))
@validated_callback_data(r"set_timeout_(?P<int32_timeout>[0-9]{1,10})")
async def callback_set_timeout_value(callback: CallbackQuery):
    """Установка значения таймаута"""
    timeout = int(callback.data.split("_")[2])
    telegram_id = callback.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if not user:
            await callback_feedback(callback, "❌ Пользователь не найден", show_alert=True)
            return

        user.lock_timeout = timeout if timeout > 0 else None
        await session.commit()

    if timeout > 0:
        await callback_feedback(callback, f"✅ Автоблокировка через {timeout} мин", show_alert=True)
    else:
        await callback_feedback(callback, "✅ Автоблокировка выключена", show_alert=True)

    await callback_settings(callback)