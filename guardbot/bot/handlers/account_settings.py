"""Account detail card, per-account settings and account notes."""

import asyncio
from html import escape
from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from guardbot.bot.callback_validation import validated_callback_data
from guardbot.bot.filters import NonCommandInputFilter
from guardbot.bot.runtime import (
    callback_feedback,
    clear_user_workflows,
    delete_secret_message_or_abort,
)
from guardbot.bot.states import NoteFlow
from guardbot.database import AsyncSessionLocal, Mafile, User
from guardbot.security import decrypt_value, encrypt_value
from guardbot.services.ownership import get_owned_group, get_owned_mafile
from guardbot.steam.client import redact_proxy_url
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = Router(name=__name__)


@router.callback_query(F.data.startswith("account_detail_"))
@validated_callback_data(r"account_detail_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_account_detail(callback: CallbackQuery):
    mafile_id = int(callback.data.split("_")[2])

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)

        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        # 🔥 Проверяем общее количество аккаунтов у пользователя
        stmt = select(Mafile).where(Mafile.telegram_id == mafile.telegram_id)
        result = await session.execute(stmt)
        all_mafiles = result.scalars().all()
        total_accounts = len(all_mafiles)

        # Проверяем статусы для отображения
        steamid_text = f"<code>{mafile.steamid}</code>" if mafile.steamid else "<i>Не привязан</i>"
        steamguard_status = "✅ Активирован" if mafile.shared_secret else "❌ Не активирован"
        token_status = "✅ Есть" if mafile.access_token else "❌ Отсутствует"
        last_login = mafile.last_login_at.strftime('%d.%m.%Y %H:%M') if mafile.last_login_at else '<i>Никогда</i>'
        token_expires = mafile.token_expires_at.strftime('%d.%m.%Y %H:%M') if mafile.token_expires_at else '<i>Неизвестно</i>'

        # Прокси информация
        if mafile.unique_proxy and mafile.proxy:
            decrypted = decrypt_value(mafile.proxy)
            redacted = escape(redact_proxy_url(decrypted)) if decrypted else "N/A"
            proxy_info = f"уникальный: <code>{redacted}</code>"
        else:
            stmt = select(User).where(User.telegram_id == mafile.telegram_id)
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()
            if user and user.general_proxy:
                decrypted = decrypt_value(user.general_proxy)
                redacted = escape(redact_proxy_url(decrypted)) if decrypted else "N/A"
                proxy_info = f"общий: <code>{redacted}</code>"
            else:
                proxy_info = "⚠️ Не настроен (только коды)"

        detail_text = (
            f"👤 <b>ДЕТАЛИ АККАУНТА</b>\n\n"
            f"📱 <b>Логин:</b> <code>{escape(mafile.account_name)}</code>\n"
            f"🆔 <b>SteamID:</b> {steamid_text}\n\n"
            f"🔐 <b>Steam Guard:</b> {steamguard_status}\n"
            f"🔑 <b>Токен доступа:</b> {token_status}\n"
            f"🌐 <b>Прокси:</b> {proxy_info}\n\n"
            f"📅 <b>Последний вход:</b> {last_login}\n"
            f"⏰ <b>Токен истекает:</b> {token_expires}\n\n"
            f"📝 <b>Заметки:</b> {escape(decrypt_value(mafile.notes) or 'Нет')}"
        )

        # 🔥 НОВАЯ КЛАВИАТУРА
        keyboard_buttons = []

        # 1. Получить код (если есть shared_secret) или варианты входа
        if mafile.shared_secret:
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text="🔐 Получить код",
                    callback_data=f"action_get_code_{mafile.id}"
                )
            ])
        else:
            # Нет Steam Guard — предлагаем варианты входа
            if mafile.password and mafile.password.strip():
                keyboard_buttons.append([
                    InlineKeyboardButton(
                        text="🔑 Войти по паролю",
                        callback_data=f"action_login_{mafile.id}"
                    )
                ])
            if mafile.refresh_token:
                keyboard_buttons.append([
                    InlineKeyboardButton(
                        text="🔄 Войти по токену",
                        callback_data=f"try_refresh_{mafile.id}"
                    )
                ])

        # 2. Подтверждения (если есть identity_secret и access_token)
        if mafile.identity_secret and mafile.access_token:
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text="✅ Подтверждения",
                    callback_data=f"confirmations_menu_{mafile.id}"
                )
            ])

        # 3. Трейды (если есть access_token)
        if mafile.access_token:
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text="🔄 Трейды",
                    callback_data=f"trades_menu_{mafile.id}"
                )
            ])

        # 4. Инвентарь (если есть steamid)
        if mafile.steamid:
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text="🎒 Инвентарь",
                    callback_data=f"inventory_menu_{mafile.id}"
                )
            ])

        # 5. Настройки аккаунта
        keyboard_buttons.append([
            InlineKeyboardButton(
                text="⚙️ Настройки аккаунта",
                callback_data=f"account_settings_{mafile.id}"
            )
        ])

        # 6. Удалить аккаунт
        keyboard_buttons.append([
            InlineKeyboardButton(
                text="🗑️ Удалить аккаунт",
                callback_data=f"delete_account_{mafile.id}"
            )
        ])

        # 7. Навигация (К списку только если больше 1 аккаунта)
        nav_buttons = []
        if total_accounts > 1:
            nav_buttons.append(
                InlineKeyboardButton(text="◀️ К списку", callback_data="my_accounts")
            )
        nav_buttons.append(
            InlineKeyboardButton(text="🏠 Главная", callback_data="back_to_start")
        )
        keyboard_buttons.append(nav_buttons)

        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

        await callback.message.edit_text(detail_text, parse_mode="HTML", reply_markup=keyboard)
        await callback_feedback(callback)


@router.callback_query(F.data.startswith("account_settings_"))
@validated_callback_data(r"account_settings_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_account_settings(callback: CallbackQuery):
    """Настройки конкретного аккаунта"""
    mafile_id = int(callback.data.split("_")[2])

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        # 🔥 Используем общую функцию обновления
        await update_account_settings_message(callback.message, mafile, session)
        await callback_feedback(callback)


@router.callback_query(F.data.startswith("toggle_unique_proxy_"))
@validated_callback_data(
    r"toggle_unique_proxy_(?P<db_id>[1-9][0-9]{0,9})_(?:on|off)"
)
async def callback_toggle_unique_proxy(callback: CallbackQuery):
    """Включение/выключение уникального прокси"""
    parts = callback.data.split("_")
    mafile_id = int(parts[3])
    action = parts[4]  # "on" или "off"

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        if action == "on":
            # Проверяем, есть ли уникальный прокси
            if not mafile.proxy:
                await callback_feedback(callback, "❌ Сначала добавьте уникальный прокси!", show_alert=True)
                return

            mafile.unique_proxy = True
            await session.commit()
            await callback_feedback(callback, "✅ Уникальный прокси включен", show_alert=True)
        else:
            mafile.unique_proxy = False
            await session.commit()
            await callback_feedback(callback, "✅ Уникальный прокси выключен, используется общий", show_alert=True)

        # 🔥 Обновляем страницу настроек с тем же callback.message
        await update_account_settings_message(callback.message, mafile, session)


async def update_account_settings_message(message: Message, mafile: Mafile, db_session: AsyncSession):
    """Обновляет сообщение с настройками аккаунта"""

    # Проверяем общее количество аккаунтов
    stmt = select(Mafile).where(Mafile.telegram_id == mafile.telegram_id)
    result = await db_session.execute(stmt)
    total_accounts = len(result.scalars().all())

    # Информация о прокси
    if mafile.unique_proxy and mafile.proxy:
        decrypted_proxy = decrypt_value(mafile.proxy)
        redacted_proxy = (
            escape(redact_proxy_url(decrypted_proxy))
            if decrypted_proxy
            else "N/A"
        )
        proxy_status = f"🔒 Уникальный прокси:\n<code>{redacted_proxy}</code>"
        unique_enabled = True
    else:
        stmt = select(User).where(User.telegram_id == mafile.telegram_id)
        result = await db_session.execute(stmt)
        user = result.scalar_one_or_none()
        if user and user.general_proxy:
            decrypted_proxy = decrypt_value(user.general_proxy)
            redacted_proxy = (
                escape(redact_proxy_url(decrypted_proxy))
                if decrypted_proxy
                else "N/A"
            )
            proxy_status = f"🌐 Общий прокси:\n<code>{redacted_proxy}</code>"
        else:
            proxy_status = "❌ Прокси не настроен"
        unique_enabled = False

    # Статус токена
    token_status = "✅ Токен есть" if mafile.access_token else "❌ Токен отсутствует"
    if mafile.token_expires_at:
        token_status += f"\n⏰ Истекает: {mafile.token_expires_at.strftime('%d.%m.%Y %H:%M')}"

    # Заметка
    note_status = (
        escape(decrypt_value(mafile.notes) or "")
        if mafile.notes
        else "<i>Нет заметки</i>"
    )

    settings_text = (
        f"⚙️ <b>НАСТРОЙКИ АККАУНТА</b>\n\n"
        f"📱 <b>Аккаунт:</b> <code>{escape(mafile.account_name)}</code>\n\n"
        f"🔑 <b>Статус токена:</b> {token_status}\n\n"
        f"{proxy_status}\n\n"
        f"📝 <b>Заметка:</b> {note_status}\n\n"
        f"<i>Для удаления уникального прокси отправьте <b>точку</b> (.) при изменении</i>"
    )

    keyboard_buttons = []

    # 1. Вход
    if mafile.password and mafile.password.strip():
        keyboard_buttons.append([
            InlineKeyboardButton(
                text="🔑 Войти по паролю",
                callback_data=f"action_login_{mafile.id}"
            )
        ])

    if mafile.refresh_token:
        keyboard_buttons.append([
            InlineKeyboardButton(
                text="🔄 Войти по токену",
                callback_data=f"try_refresh_{mafile.id}"
            )
        ])

    # 2. Проверить токен
    if mafile.refresh_token:
        keyboard_buttons.append([
            InlineKeyboardButton(
                text="🔄 Проверить токен",
                callback_data=f"action_check_token_{mafile.id}"
            )
        ])

    # 3. Вкл/Выкл уникальный прокси
    if unique_enabled:
        keyboard_buttons.append([
            InlineKeyboardButton(
                text="🔴 Выключить уникальный прокси",
                callback_data=f"toggle_unique_proxy_{mafile.id}_off"
            )
        ])
    else:
        keyboard_buttons.append([
            InlineKeyboardButton(
                text="🟢 Включить уникальный прокси",
                callback_data=f"toggle_unique_proxy_{mafile.id}_on"
            )
        ])

    # 4. Изменить/Добавить уникальный прокси
    if mafile.proxy:
        button_text = "✏️ Изменить уникальный прокси"
    else:
        button_text = "➕ Добавить уникальный прокси"

    keyboard_buttons.append([
        InlineKeyboardButton(
            text=button_text,
            callback_data=f"change_unique_proxy_{mafile.id}"
        )
    ])

    # 5. Проверить прокси
    keyboard_buttons.append([
        InlineKeyboardButton(
            text="🧪 Проверить прокси",
            callback_data=f"test_account_proxy_{mafile.id}"
        )
    ])

    # 🔥 5. Закрепление
    if mafile.is_pinned:
        keyboard_buttons.append([
            InlineKeyboardButton(
                text="📌 Открепить из списка",
                callback_data=f"toggle_pin_{mafile.id}"
            )
        ])
    else:
        keyboard_buttons.append([
            InlineKeyboardButton(
                text="📍 Закрепить в списке",
                callback_data=f"toggle_pin_{mafile.id}"
            )
        ])

    # 🔥 6. Группа
    current_group_name = "Без группы"
    if mafile.group_id:
        grp = await get_owned_group(db_session, mafile.group_id, mafile.telegram_id)
        if grp:
            current_group_name = grp.name
    keyboard_buttons.append([
        InlineKeyboardButton(
            text=f"📂 Группа: {current_group_name}",
            callback_data=f"change_group_{mafile.id}"
        )
    ])

    # 🔥 7. Изменить заметку
    keyboard_buttons.append([
        InlineKeyboardButton(
            text="📝 Изменить заметку",
            callback_data=f"change_note_{mafile.id}"
        )
    ])
    # 🔥 8. Экспорт mafile
    keyboard_buttons.append([
        InlineKeyboardButton(
            text="📤 Экспорт mafile",
            callback_data=f"export_mafile_{mafile.id}"
        )
    ])
    # Навигация
    nav_buttons = []
    if total_accounts > 1:
        nav_buttons.append(
            InlineKeyboardButton(text="◀️ К списку", callback_data="my_accounts")
        )
    nav_buttons.append(
        InlineKeyboardButton(text="👤 К аккаунту", callback_data=f"account_detail_{mafile.id}")
    )
    keyboard_buttons.append(nav_buttons)

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    await message.edit_text(settings_text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("change_note_"))
@validated_callback_data(r"change_note_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_change_note(callback: CallbackQuery, state: FSMContext):
    """Запрос на изменение заметки к аккаунту"""
    mafile_id = int(callback.data.split("_")[2])

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        account_name = mafile.account_name
        current_note = decrypt_value(mafile.notes) or "нет заметки"

    text = (
        "📝 <b>ИЗМЕНЕНИЕ ЗАМЕТКИ</b>\n\n"
        f"📱 Аккаунт: <code>{escape(account_name)}</code>\n"
        f"📝 Текущая заметка: <i>{escape(current_note)}</i>\n\n"
        "<b>Отправьте новую заметку:</b>\n\n"
        "<i>Для удаления заметки отправьте точку <code>.</code></i>\n\n"
        "Для отмены нажмите кнопку ниже:"
    )

    await clear_user_workflows(callback.from_user.id, state)
    try:
        await state.set_state(NoteFlow.waiting_note)
        await state.set_data({"mafile_id": mafile_id})
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=f"cancel_state_to_settings_{mafile_id}",
                    )]
                ]
            ),
        )
    except (Exception, asyncio.CancelledError):
        await clear_user_workflows(callback.from_user.id, state)
        raise
    await callback_feedback(callback)


@router.message(NoteFlow.waiting_note, F.text, ~F.text.startswith("/"))
async def process_account_note(message: Message, state: FSMContext):
    """Persist an encrypted account note from the typed flow."""
    if message.chat.type != ChatType.PRIVATE:
        await message.answer(
            "❌ Заметку можно вводить только в личном чате с ботом."
        )
        return

    if not await delete_secret_message_or_abort(message):
        return

    note_text = message.text.strip()
    state_data = await state.get_data()
    mafile_id = state_data.get("mafile_id")

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, message.from_user.id)
        if not mafile:
            await clear_user_workflows(message.from_user.id, state)
            await message.answer("❌ Аккаунт не найден")
            return

        if note_text == ".":
            mafile.notes = None
            action = "удалена"
            note_value = ""
        else:
            note_text = note_text[:500]
            mafile.notes = encrypt_value(note_text)
            action = "сохранена"
            note_value = f"\n📝 Заметка: <i>{escape(note_text)}</i>"

        await session.commit()
        await clear_user_workflows(message.from_user.id, state)

        settings_msg = await message.answer(
            "⚙️ <b>Загружаю настройки...</b>",
            parse_mode="HTML",
        )
        await update_account_settings_message(settings_msg, mafile, session)

        await message.answer(
            f"✅ <b>Заметка {action}!</b>\n\n"
            f"📱 Аккаунт: <code>{escape(mafile.account_name)}</code>{note_value}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="👤 К настройкам аккаунта",
                        callback_data=f"account_settings_{mafile.id}",
                    )]
                ]
            ),
        )


@router.message(NoteFlow.waiting_note, NonCommandInputFilter())
async def reject_note_non_text(message: Message, state: FSMContext) -> None:
    """Keep note documents from falling through to the legacy import handler."""
    if message.chat.type != ChatType.PRIVATE:
        return
    if not await delete_secret_message_or_abort(message):
        return
    await message.answer(
        "❌ Отправьте заметку текстом, точку для удаления или /cancel."
    )
