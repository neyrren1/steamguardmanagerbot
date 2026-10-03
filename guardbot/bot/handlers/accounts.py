"""Extracted from the legacy bot module without behavior changes."""

from aiogram import F
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from datetime import datetime
from guardbot.bot.runtime import check_proxy_available, dp, require_proxy, user_states
from guardbot.config import STEAM_CURRENCIES, logger
from guardbot.database import AccountGroup, AsyncSessionLocal, Mafile, User
from guardbot.security import decrypt_value
from guardbot.services.session_manager import SteamSessionManager
from guardbot.steam.client import AsyncSteamMobile, InvalidCredentials, InvalidSteamGuardCode, LoginConfirmType, generate_device_id
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
import aiohttp
import time

@dp.callback_query(F.data == "info")
async def callback_info(callback: CallbackQuery):
    user = callback.from_user

    info_text = (
        "ℹ️ <b>ДЕТАЛЬНАЯ ИНФОРМАЦИЯ</b>\n\n"
        "<b>Steam Guard Manager Bot v1.0</b>\n\n"
        "Этот бот создан для удобного управления Steam Guard кодами и аккаунтами Steam.\n\n"
        "<b>📌 Возможности:</b>\n"
        "• Хранение неограниченного количества Steam аккаунтов\n"
        "• Генерация Steam Guard кодов в реальном времени\n"
        "• Просмотр необходимых подтверждений на аккаунте\n"
        "• Просмотр трейдов(в том числе принятие, отклонение, уведомления о новых трейдах)\n"
        "• Просмотр инвентарей своего аккаунта(в том числе просмотр детальной информации о предмете и выставление на продажу)\n"
        "• Возможность объединять аккаунты по группам\n"
        "• Массовый импорт/экспорт аккаунтов\n"
        "• Возможность поставить на бота пароль с автоблокировкой по истечению времени\n"
        "• Автоматический вход и обновление токенов\n\n"
        "<b>🔒 Безопасность:</b>\n"
        "• Каждый пользователь имеет доступ только к своим аккаунтам\n"
        "• Пароли и токены не передаются третьим лицам\n"
        "• Все данные хранятся в защищенной зашифрованной базе данных\n\n"
        "<b>📊 Ваша статистика:</b>\n"
        "• Telegram ID: <code>" + str(user.id) + "</code>\n"
        "• Username: @" + (user.username or 'не указан') + "\n\n"
        "<b>⚡ Используйте кнопки меню для навигации</b>"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
        ]
    )

    await callback.message.edit_text(info_text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data == "instruction")
async def callback_instruction(callback: CallbackQuery):
    instruction_text = (
        "<b>🌐 Настройка прокси (ОБЯЗАТЕЛЬНО):</b>\n"
        "• В боте есть 2 вида прокси - общая и уникальная. Уникальная будет использоваться на каждом аккаунте, на котором вы ее выставите и включите. Общая будет использоваться на всех аккаунтах, где не установлеа либо выключена уникальная прокси. Для пользования достаточно добавить одну общую прокси и использовать на ней все аккаунты.\n"
        "• <code>/set_proxy [proxy_url]</code> - общий прокси\n"
        "• <code>/set_account_proxy [acc] [proxy]</code> - уникальный прокси\n"
        "• <code>/my_proxy</code> - посмотреть настройки\n\n"
        "📚 <b>ИНСТРУКЦИЯ ПО ИСПОЛЬЗОВАНИЮ</b>\n\n"
        "<b>📝 Добавление аккаунта:</b>\n"
        "1. Нажмите /add или кнопку \"Добавить аккаунт\"\n"
        "2. Отправьте данные в формате: <code>login:password</code>\n"
        "2.5. Отправьте свой <code>mafile</code> от аккаунта, если имеется\n"
        "3. После добавления выполните вход: <code>/login логин</code>\n"
        "4. При необходимости введите код из email/Steam Guard\n\n"
        "<b>🔐 Получение Steam Guard кода:</b>\n"
        "• Команда: <code>/gc [имя_аккаунта]</code>\n\n"
        "<b>📋 Управление аккаунтами:</b>\n"
        "• <code>/add</code> - добавить аккаунт\n"
        "• <code>/accounts</code> - список всех аккаунтов\n"
        "• <code>/login [аккаунт]</code> - войти в аккаунт\n"
        "• <code>/delete [аккаунт]</code> - удалить аккаунт\n\n"
        "<b>💡 Советы:</b>\n"
        "• Регулярно проверяйте статус токенов\n"
        "• При истечении токена просто выполните /login\n\n"
        "<b>🆘 Нужна помощь?</b>\n"
        "Обратитесь к администратору бота @scaredycataaa"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
        ]
    )

    await callback.message.edit_text(instruction_text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "settings")
async def callback_settings(callback: CallbackQuery):
    """Настройки бота"""
    telegram_id = callback.from_user.id

    # Проверяем статус блокировки
    async with AsyncSessionLocal() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        has_password = bool(user and user.bot_password)
        lock_enabled = bool(user and user.lock_timeout)

        lock_status = f"🔒 Таймаут: {user.lock_timeout} мин" if lock_enabled else "🔓 Выключено"
        password_status = "✅ Установлен" if has_password else "❌ Не установлен"

    settings_text = (
        "⚙️ <b>НАСТРОЙКИ</b>\n\n"
        "<b>🔒 Безопасность:</b>\n"
        f"├─ Пароль: {password_status}\n"
        f"└─ Автоблокировка: {lock_status}\n\n"
        "<b>🌐 Управление прокси:</b>\n"
        "<code>/set_proxy [url]</code> — общий прокси\n"
        "<code>/set_account_proxy [acc] [url]</code> — уникальный прокси\n"
        "<code>/remove_account_proxy [acc]</code> — удалить уникальный прокси\n"
        "<code>/my_proxy</code> — посмотреть настройки прокси\n\n"
        "🚧 <b>Другие настройки в разработке</b> 🚧"
    )

    keyboard_buttons = []

    # Кнопки управления паролем
    if has_password:
        keyboard_buttons.append([
            InlineKeyboardButton(
                text="🔑 Изменить пароль",
                callback_data="change_password"
            ),
            InlineKeyboardButton(
                text="🗑️ Удалить пароль",
                callback_data="remove_password"
            )
        ])
        keyboard_buttons.append([
            InlineKeyboardButton(
                text="🔒 Заблокировать бота",
                callback_data="lock_bot"
            )
        ])
    else:
        keyboard_buttons.append([
            InlineKeyboardButton(
                text="🔑 Установить пароль",
                callback_data="set_password"
            )
        ])

    # Кнопка настройки автоблокировки
    keyboard_buttons.append([
        InlineKeyboardButton(
            text="⏱ Настроить автоблокировку",
            callback_data="set_lock_timeout"
        )
    ])

    keyboard_buttons.append([
        InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    await callback.message.edit_text(settings_text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(F.data == "mafile_instruction")
async def callback_mafile_instruction(callback: CallbackQuery):
    """Инструкция по импорту mafile"""
    await callback.message.edit_text(
        "📁 <b>ИМПОРТ MAFILE ФАЙЛОВ</b>\n\n"
        "Просто отправьте мне один или несколько файлов <b>.mafile</b>!\n\n"
        "<b>Как это работает:</b>\n"
        "• Отправьте файлы прямо в чат\n"
        "• Бот автоматически обработает каждый файл\n"
        "• Если аккаунт уже есть - обновит Steam Guard\n"
        "• Если аккаунта нет - создаст новый\n\n"
        "<b>⚠️ Внимание:</b>\n"
        "При создании нового аккаунта пароль не сохраняется.\n"
        "Добавьте пароль позже в деталях аккаунта.\n\n"
        "Отправляйте файлы прямо сейчас!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_start")]
            ]
        )
    )
    await callback.answer()

@dp.callback_query(F.data == "my_accounts")
async def callback_my_accounts(callback: CallbackQuery):
    """Список аккаунтов с пагинацией"""
    await show_accounts_page(callback, page=0)


@dp.callback_query(F.data.startswith("accounts_page_"))
async def callback_accounts_page(callback: CallbackQuery):
    """Переключение страниц аккаунтов"""
    page = int(callback.data.split("_")[2])
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
            grp = await session.get(AccountGroup, group_id)
            if grp:
                group_name = f"Группа: {grp.name}"

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


@dp.callback_query(F.data == "page_info")
async def callback_page_info(callback: CallbackQuery):
    """Заглушка для кнопки с номером страницы"""
    await callback.answer("Это текущая страница", show_alert=False)

@dp.callback_query(F.data.startswith("account_detail_"))
async def callback_account_detail(callback: CallbackQuery):
    mafile_id = int(callback.data.split("_")[2])

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.id == mafile_id)
        result = await session.execute(stmt)
        mafile = result.scalar_one_or_none()

        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
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
            proxy_info = f"уникальный: <code>{decrypted[:30] if decrypted else 'N/A'}...</code>"
        else:
            stmt = select(User).where(User.telegram_id == mafile.telegram_id)
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()
            if user and user.general_proxy:
                decrypted = decrypt_value(user.general_proxy)
                proxy_info = f"общий: <code>{decrypted[:30] if decrypted else 'N/A'}...</code>"
            else:
                proxy_info = "⚠️ Не настроен (только коды)"

        detail_text = (
            f"👤 <b>ДЕТАЛИ АККАУНТА</b>\n\n"
            f"📱 <b>Логин:</b> <code>{mafile.account_name}</code>\n"
            f"🆔 <b>SteamID:</b> {steamid_text}\n\n"
            f"🔐 <b>Steam Guard:</b> {steamguard_status}\n"
            f"🔑 <b>Токен доступа:</b> {token_status}\n"
            f"🌐 <b>Прокси:</b> {proxy_info}\n\n"
            f"📅 <b>Последний вход:</b> {last_login}\n"
            f"⏰ <b>Токен истекает:</b> {token_expires}\n\n"
            f"📝 <b>Заметки:</b> {decrypt_value(mafile.notes) or 'Нет'}"
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
        await callback.answer()

@dp.callback_query(F.data.startswith("account_settings_"))
async def callback_account_settings(callback: CallbackQuery):
    """Настройки конкретного аккаунта"""
    mafile_id = int(callback.data.split("_")[2])

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        # 🔥 Используем общую функцию обновления
        await update_account_settings_message(callback.message, mafile, session)
        await callback.answer()

@dp.callback_query(F.data.startswith("toggle_unique_proxy_"))
async def callback_toggle_unique_proxy(callback: CallbackQuery):
    """Включение/выключение уникального прокси"""
    parts = callback.data.split("_")
    mafile_id = int(parts[3])
    action = parts[4]  # "on" или "off"

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        if action == "on":
            # Проверяем, есть ли уникальный прокси
            if not mafile.proxy:
                await callback.answer("❌ Сначала добавьте уникальный прокси!", show_alert=True)
                return

            mafile.unique_proxy = True
            await session.commit()
            await callback.answer("✅ Уникальный прокси включен", show_alert=True)
        else:
            mafile.unique_proxy = False
            await session.commit()
            await callback.answer("✅ Уникальный прокси выключен, используется общий", show_alert=True)

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
        proxy_status = f"🔒 Уникальный прокси:\n<code>{decrypt_value(mafile.proxy)}</code>"
        unique_enabled = True
    else:
        stmt = select(User).where(User.telegram_id == mafile.telegram_id)
        result = await db_session.execute(stmt)
        user = result.scalar_one_or_none()
        if user and user.general_proxy:
            proxy_status = f"🌐 Общий прокси:\n<code>{decrypt_value(user.general_proxy)}</code>"
        else:
            proxy_status = "❌ Прокси не настроен"
        unique_enabled = False

    # Статус токена
    token_status = "✅ Токен есть" if mafile.access_token else "❌ Токен отсутствует"
    if mafile.token_expires_at:
        token_status += f"\n⏰ Истекает: {mafile.token_expires_at.strftime('%d.%m.%Y %H:%M')}"

    # Заметка
    note_status = decrypt_value(mafile.notes) if mafile.notes else "<i>Нет заметки</i>"

    settings_text = (
        f"⚙️ <b>НАСТРОЙКИ АККАУНТА</b>\n\n"
        f"📱 <b>Аккаунт:</b> <code>{mafile.account_name}</code>\n\n"
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
        grp = await db_session.get(AccountGroup, mafile.group_id)
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

@dp.callback_query(F.data.startswith("change_note_"))
async def callback_change_note(callback: CallbackQuery):
    """Запрос на изменение заметки к аккаунту"""
    mafile_id = int(callback.data.split("_")[2])

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        current_note = decrypt_value(mafile.notes) or "нет заметки"

        user_states[callback.from_user.id] = {
            "state": "waiting_note",
            "mafile_id": mafile.id,
            "account_name": mafile.account_name
        }

        text = (
            f"📝 <b>ИЗМЕНЕНИЕ ЗАМЕТКИ</b>\n\n"
            f"📱 Аккаунт: <code>{mafile.account_name}</code>\n"
            f"📝 Текущая заметка: <i>{current_note}</i>\n\n"
            f"<b>Отправьте новую заметку:</b>\n\n"
            f"<i>Для удаления заметки отправьте точку <code>.</code></i>\n\n"
            f"Для отмены нажмите кнопку ниже:"
        )

        # 🔥 Создаем callback_data для отмены, который очистит состояние
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=f"cancel_state_to_settings_{mafile.id}"
                    )]
                ]
            )
        )
        await callback.answer()

@dp.callback_query(F.data.startswith("change_unique_proxy_"))
async def callback_change_unique_proxy(callback: CallbackQuery):
    """Запрос на изменение/добавление уникального прокси"""
    mafile_id = int(callback.data.split("_")[3])

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        current_proxy = mafile.proxy or "не установлен"

        user_states[callback.from_user.id] = {
            "state": "waiting_unique_proxy",
            "mafile_id": mafile.id,
            "account_name": mafile.account_name
        }

        text = (
            f"🔒 <b>НАСТРОЙКА УНИКАЛЬНОГО ПРОКСИ</b>\n\n"
            f"📱 Аккаунт: <code>{mafile.account_name}</code>\n"
            f"🌐 Текущий прокси: <code>{current_proxy}</code>\n\n"
            f"<b>Отправьте новый прокси в формате:</b>\n"
            f"<code>protocol://username:password@host:port</code>\n\n"
            f"<b>Примеры:</b>\n"
            f"• <code>http://user:pass@192.168.1.1:8080</code>\n"
            f"• <code>socks5://user:pass@192.168.1.1:1080</code>\n\n"
            f"<b>Для удаления прокси отправьте точку:</b> <code>.</code>\n\n"
            f"Для отмены нажмите кнопку ниже:"
        )

        # 🔥 Создаем callback_data для отмены, который очистит состояние
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=f"cancel_state_to_settings_{mafile.id}"
                    )]
                ]
            )
        )
        await callback.answer()

@dp.callback_query(F.data.startswith("cancel_state_to_settings_"))
async def callback_cancel_state_to_settings(callback: CallbackQuery):
    """Отмена текущего действия и возврат в настройки аккаунта"""
    mafile_id = int(callback.data.split("_")[4])

    # 🔥 Очищаем состояние пользователя
    user_id = callback.from_user.id
    if user_id in user_states:
        user_states.pop(user_id)
        logger.info(f"Cleared state for user {user_id}")

    # Возвращаемся в настройки аккаунта
    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        await update_account_settings_message(callback.message, mafile, session)
        await callback.answer("❌ Действие отменено")

@dp.callback_query(F.data.startswith("set_unique_proxy_"))
async def callback_set_unique_proxy(callback: CallbackQuery):
    """Запрос на установку уникального прокси"""
    mafile_id = int(callback.data.split("_")[3])

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        user_states[callback.from_user.id] = {
            "state": "waiting_unique_proxy",
            "mafile_id": mafile.id,
            "account_name": mafile.account_name
        }

        await callback.message.edit_text(
            f"🔒 <b>Установка уникального прокси</b>\n\n"
            f"Аккаунт: <code>{mafile.account_name}</code>\n\n"
            f"Отправьте прокси в формате:\n"
            f"<code>protocol://username:password@host:port</code>\n\n"
            f"Примеры:\n"
            f"• <code>http://user:pass@192.168.1.1:8080</code>\n"
            f"• <code>socks5://user:pass@192.168.1.1:1080</code>\n\n"
            f"Для отмены нажмите кнопку ниже:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=f"account_settings_{mafile.id}"
                    )]
                ]
            )
        )
        await callback.answer()

@dp.callback_query(F.data.startswith("remove_account_proxy_"))
async def callback_remove_account_proxy(callback: CallbackQuery):
    """Удаление уникального прокси с аккаунта"""
    mafile_id = int(callback.data.split("_")[3])

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        mafile.unique_proxy = False
        mafile.proxy = None
        await session.commit()

        await callback.answer("✅ Уникальный прокси удален, используется общий", show_alert=True)
        await callback_account_settings(callback)

@dp.callback_query(F.data.startswith("test_account_proxy_"))
async def callback_test_account_proxy(callback: CallbackQuery):
    """Тестирование прокси аккаунта"""
    mafile_id = int(callback.data.split("_")[3])

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        # 🔥 Определяем и расшифровываем прокси
        proxy = None
        proxy_type = "Общий"

        if mafile.unique_proxy and mafile.proxy:
            proxy = decrypt_value(mafile.proxy)
            proxy_type = "Уникальный"
        else:
            stmt = select(User).where(User.telegram_id == mafile.telegram_id)
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()
            if user and user.general_proxy:
                proxy = decrypt_value(user.general_proxy)
            else:
                await callback.answer("❌ Прокси не настроен!", show_alert=True)
                return

        await callback.answer("🔄 Тестирую прокси...")

        status_msg = await callback.message.answer(
            f"🔄 <b>Тестирую {proxy_type.lower()} прокси...</b>",
            parse_mode="HTML"
        )

        try:
            test_session = aiohttp.ClientSession()

            try:
                async with test_session.get(
                    'http://httpbin.org/ip',
                    proxy=proxy,
                    ssl=False,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    ip_data = await resp.json()
                    external_ip = ip_data.get('origin', 'Unknown')

                await status_msg.edit_text(
                    f"✅ <b>{proxy_type} прокси работает!</b>\n\n"
                    f"📱 Аккаунт: <code>{mafile.account_name}</code>\n"
                    f"🌐 Прокси: <code>{proxy}</code>\n"
                    f"📍 Внешний IP: <code>{external_ip}</code>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[
                            InlineKeyboardButton(
                                text="◀️ Назад",
                                callback_data=f"account_settings_{mafile.id}"
                            )
                        ]]
                    )
                )
            finally:
                await test_session.close()

        except Exception as e:
            await status_msg.edit_text(
                f"❌ <b>Ошибка прокси:</b>\n\n<code>{str(e)[:200]}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(
                            text="◀️ Назад",
                            callback_data=f"account_settings_{mafile.id}"
                        )
                    ]]
                )
            )
        finally:
            await callback.answer()

@dp.callback_query(F.data.startswith("action_get_code_"))
async def callback_action_get_code(callback: CallbackQuery):
    """Получение кода по кнопке из деталей аккаунта"""
    mafile_id = int(callback.data.split("_")[3])

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.id == mafile_id)
        result = await session.execute(stmt)
        mafile = result.scalar_one_or_none()

        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        if not mafile.shared_secret:
            await callback.answer("Steam Guard не активирован", show_alert=True)
            return

        await callback.answer("Генерирую код...")
        await generate_and_send_code(callback.message, mafile)

@dp.callback_query(F.data.startswith("action_login_"))
async def callback_action_login(callback: CallbackQuery):
    """Вход в аккаунт по паролю (полный вход)"""
    mafile_id = int(callback.data.split("_")[2])
    telegram_id = callback.from_user.id

    # Проверяем наличие прокси
    has_proxy, _ = await check_proxy_available(telegram_id, for_steam_requests=True)
    if not has_proxy:
        await callback.answer("❌ Нет доступного прокси!", show_alert=True)
        await callback.message.answer(
            "❌ <b>Нет доступного прокси!</b>\n\n"
            "Настройте прокси для входа в аккаунт.\n\n"
            "<code>/set_proxy http://user:pass@host:port</code>",
            parse_mode="HTML"
        )
        return

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)

        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        # Проверяем наличие пароля
        if not mafile.password or not mafile.password.strip():
            await callback.message.edit_text(
                f"⚠️ <b>Пароль отсутствует!</b>\n\n"
                f"Аккаунт: <code>{mafile.account_name}</code>\n\n"
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
            await callback.answer()
            return

        # Пароль есть - выполняем ПОЛНЫЙ вход
        await callback.answer(f"Вхожу в {mafile.account_name}...")
        await perform_login_full(callback.message, mafile, session)

async def perform_login_full(message: Message, mafile: Mafile, db_session: AsyncSession):
    """Полный вход по логину:паролю (без попытки refresh_token)"""
    telegram_id = message.from_user.id

    status_msg = await message.answer(
        f"🔄 <b>Выполняю вход в {mafile.account_name}...</b>",
        parse_mode="HTML"
    )

    client = await SteamSessionManager.create_steam_client_from_db(mafile, db_session)
    client_to_close = True

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
                logger.warning(f"Could not detect country: {e}")

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

        elif code_type == LoginConfirmType.email:
            # Требуется код с почты
            user_states[telegram_id] = {
                "state": "waiting_email_code",
                "account_name": mafile.account_name,
                "client": client,
                "mafile_id": mafile.id,
                "db_session": db_session
            }
            client_to_close = False

            await status_msg.edit_text(
                f"📧 <b>Требуется код с EMAIL</b>\n\n"
                f"Steam отправил код подтверждения на вашу почту.\n"
                f"ℹ️ <i>На аккаунте НЕТ мобильного Steam Guard</i>\n\n"
                f"<b>Введите код из письма:</b>\n\n"
                f"💡 <i>После входа вы сможете подключить Steam Guard через бота!</i>",
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
            return

        elif code_type == LoginConfirmType.mobile:
            if mafile.shared_secret:
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
                        logger.warning(f"Could not detect country: {e}")

                    await SteamSessionManager.save_steam_session_to_db(db_session, mafile.id, client)

                    await status_msg.edit_text(
                        f"✅ <b>Успешный вход!</b>\n"
                        f"SteamID: <code>{client.steamid}</code>\n\n"
                        f"🔐 Steam Guard активирован!\n"
                        f"Используйте <code>/gc {mafile.account_name}</code> для получения кодов.",
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

            else:
                # Нет shared_secret — просим mafile или код вручную
                user_states[telegram_id] = {
                    "state": "waiting_steam_code_or_mafile",
                    "account_name": mafile.account_name,
                    "client": client,
                    "mafile_id": mafile.id,
                    "db_session": db_session
                }
                client_to_close = False

                await status_msg.edit_text(
                    f"⚠️ <b>На аккаунте уже подключен Steam Guard!</b>\n\n"
                    f"У бота нет ключей для генерации кода.\n\n"
                    f"<b>Выберите действие:</b>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="📤 Импортировать mafile",
                                callback_data=f"import_mafile_for_{mafile.id}"
                            )],
                            [InlineKeyboardButton(
                                text="✏️ Ввести код вручную",
                                callback_data=f"manual_code_{mafile.id}"
                            )],
                            [InlineKeyboardButton(
                                text="❌ Отмена",
                                callback_data=f"account_detail_{mafile.id}"
                            )]
                        ]
                    )
                )
                return

    except InvalidCredentials:
        await status_msg.edit_text("❌ <b>Неверный логин или пароль</b>", parse_mode="HTML")
    except Exception as e:
        await status_msg.edit_text(f"❌ Ошибка при входе: {str(e)}")
        logger.error(f"Login error for {mafile.account_name}: {e}", exc_info=True)
    finally:
        if client_to_close:
            try:
                await client.close()
            except:
                pass

@dp.callback_query(F.data.startswith("enter_password_"))
async def callback_enter_password(callback: CallbackQuery):
    """Запрос пароля для входа"""
    mafile_id = int(callback.data.split("_")[2])

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.id == mafile_id)
        result = await session.execute(stmt)
        mafile = result.scalar_one_or_none()

        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        user_states[callback.from_user.id] = {
            "state": "waiting_password_for_login",
            "mafile_id": mafile.id,
            "account_name": mafile.account_name
        }

        await callback.message.edit_text(
            f"🔑 <b>Введите пароль для {mafile.account_name}:</b>\n\n"
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
        await callback.answer()

@dp.callback_query(F.data.startswith("try_refresh_"))
async def callback_try_refresh(callback: CallbackQuery):
    """Попытка входа через refresh token без пароля"""
    mafile_id = int(callback.data.split("_")[2])

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.id == mafile_id)
        result = await session.execute(stmt)
        mafile = result.scalar_one_or_none()

        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        if not mafile.refresh_token:
            await callback.answer("Refresh token отсутствует", show_alert=True)
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

        await callback.answer("Пробую восстановить сессию...")

        status_msg = await callback.message.edit_text(
            f"🔄 <b>Пробую восстановить сессию для {mafile.account_name}...</b>",
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
                f"<code>{str(e)}</code>\n\n"
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

@dp.callback_query(F.data.startswith("manual_code_"))
async def callback_manual_code(callback: CallbackQuery):
    """Ручной ввод Steam Guard кода"""
    mafile_id = int(callback.data.split("_")[2])
    telegram_id = callback.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.id == mafile_id)
        result = await session.execute(stmt)
        mafile = result.scalar_one_or_none()

        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            code_type = await client.login()

            user_states[telegram_id] = {
                "state": "waiting_steam_code_manual",
                "account_name": mafile.account_name,
                "client": client,
                "mafile_id": mafile.id,
                "db_session": session
            }

            await callback.message.edit_text(
                f"📱 <b>Введите Steam Guard код для {mafile.account_name}:</b>\n\n"
                f"Код из приложения Steam / SDA",
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
            await callback.answer()

        except Exception as e:
            await callback.message.edit_text(f"❌ Ошибка: {str(e)}")
            await callback.answer()
            await client.close()


@dp.callback_query(F.data.startswith("import_mafile_for_"))
async def callback_import_mafile_for(callback: CallbackQuery):
    """Импорт mafile для аккаунта"""
    mafile_id = int(callback.data.split("_")[3])

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.id == mafile_id)
        result = await session.execute(stmt)
        mafile = result.scalar_one_or_none()

        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        user_states[callback.from_user.id] = {
            "state": "waiting_mafile_for_account",
            "mafile_id": mafile.id,
            "account_name": mafile.account_name
        }

        await callback.message.edit_text(
            f"📤 <b>ИМПОРТ STEAM GUARD</b>\n\n"
            f"Аккаунт: <b>{mafile.account_name}</b>\n\n"
            f"Отправьте файл <b>.mafile</b> с ключами Steam Guard.\n\n"
            f"<b>Где взять?</b>\n"
            f"• Steam Desktop Authenticator (SDA)\n"
            f"• Мобильный Steam (извлечь)\n\n"
            f"Отправьте файл или /cancel для отмены.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена", callback_data=f"account_detail_{mafile.id}")]
                ]
            )
        )
        await callback.answer()

@dp.callback_query(F.data.startswith("action_check_token_"))
async def callback_action_check_token(callback: CallbackQuery):
    """Проверка токена по кнопке"""
    mafile_id = int(callback.data.split("_")[3])

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.id == mafile_id)
        result = await session.execute(stmt)
        mafile = result.scalar_one_or_none()

        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        await callback.answer("Проверяю токен...")

        if not mafile.refresh_token:
            await callback.message.answer(
                f"⚠️ <b>{mafile.account_name}</b>: Нет refresh token",
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
                    f"❌ <b>{mafile.account_name}</b>: Токен истек\n"
                    f"Требуется повторный вход: нажмите кнопку «Войти в аккаунт»",
                    parse_mode="HTML"
                )
            else:
                expire_time = datetime.fromtimestamp(
                    client.get_token_expire_timestamp(refresh_token)
                )
                days_left = (expire_time - datetime.utcnow()).days

                await callback.message.answer(
                    f"✅ <b>{mafile.account_name}</b>: Токен действителен\n"
                    f"Истекает: {expire_time.strftime('%d.%m.%Y %H:%M')}\n"
                    f"Осталось дней: {days_left}",
                    parse_mode="HTML"
                )
        except Exception as e:
            await callback.message.answer(
                f"⚠️ <b>{mafile.account_name}</b>: Не удалось проверить токен\n"
                f"<code>{str(e)}</code>",
                parse_mode="HTML"
            )
        finally:
            await client.close()

@dp.callback_query(F.data.startswith("delete_account_"))
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
    await callback.answer()

@dp.callback_query(F.data.startswith("confirm_delete_"))
async def callback_confirm_delete(callback: CallbackQuery):
    mafile_id = int(callback.data.split("_")[2])

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.id == mafile_id)
        result = await session.execute(stmt)
        mafile = result.scalar_one_or_none()

        if mafile:
            account_name = mafile.account_name
            await session.delete(mafile)
            await session.commit()

            await callback.message.edit_text(
                "✅ Аккаунт <b>" + account_name + "</b> успешно удален!",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="📋 К списку аккаунтов", callback_data="my_accounts")],
                        [InlineKeyboardButton(text="🏠 На главную", callback_data="back_to_start")]
                    ]
                )
            )
        else:
            await callback.answer("Аккаунт не найден", show_alert=True)

    await callback.answer()

@dp.callback_query(F.data == "add_account")
async def callback_add_account(callback: CallbackQuery):
    add_text = (
        "➕ <b>ДОБАВЛЕНИЕ АККАУНТА</b>\n\n"
        "Отправьте данные в формате:\n"
        "<code>login:password</code>\n\n"
        "Например: <code>mylogin:mypassword</code>"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_state_to_accounts")]  # 🔥 Изменено
        ]
    )

    await callback.message.edit_text(add_text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()

    user_states[callback.from_user.id] = {"state": "waiting_steam_credentials"}

@dp.callback_query(F.data == "cancel_state_to_accounts")
async def callback_cancel_state_to_accounts(callback: CallbackQuery):
    """Отмена добавления аккаунта и возврат к списку"""
    user_id = callback.from_user.id
    if user_id in user_states:
        user_states.pop(user_id)
        logger.info(f"Cleared state for user {user_id}")

    # Возвращаемся к списку аккаунтов
    await callback_my_accounts(callback)
    await callback.answer("❌ Добавление отменено")

@dp.message(Command("add_steam"))
@dp.message(Command("add"))
@dp.message(Command("new"))
@require_proxy
async def cmd_add_steam(message: Message, **kwargs):  # добавить **kwargs
    await message.answer(
        "📝 Для добавления Steam аккаунта, отправьте данные в формате:\n\n"
        "<code>account_name:password</code>\n\n"
        "Например: <code>mylogin:mypassword</code>",
        parse_mode="HTML"
    )
    user_states[message.from_user.id] = {"state": "waiting_steam_credentials"}

@dp.message(Command("my_accounts"))
@dp.message(Command("accounts"))
@dp.message(Command("list"))
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

@dp.message(Command("get_code"))
@dp.message(Command("getcode"))
@dp.message(Command("guardcode"))
@dp.message(Command("guard_code"))
@dp.message(Command("gc"))
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
                f"❌ Аккаунт <b>{account_input}</b> не найден или не имеет Steam Guard",
                parse_mode="HTML"
            )
            return

        await generate_and_send_code(message, mafile)

async def generate_and_send_code(message: Message, mafile: Mafile):
    """Генерация и отправка кода с красивым интерфейсом"""
    status_msg = await message.answer(
        f"🔄 <b>Генерирую код для {mafile.account_name}...</b>",
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

        await client._ensure_session()

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
            f"<b>📱 Аккаунт:</b> <code>{mafile.account_name}</code>\n"
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
            f"❌ Ошибка при генерации кода:\n<code>{str(e)}</code>",
            parse_mode="HTML"
        )
        logger.error(f"Ошибка генерации кода для {mafile.account_name}: {e}")
    finally:
        await client.close()

@dp.callback_query(F.data.startswith("refresh_code_"))
async def callback_refresh_code(callback: CallbackQuery):
    """Обновление кода (редактирует существующее сообщение)"""
    mafile_id = int(callback.data.split("_")[2])

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.id == mafile_id)
        result = await session.execute(stmt)
        mafile = result.scalar_one_or_none()

        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        await callback.answer("Обновляю...")

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

            await client._ensure_session()

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
                f"<b>📱 Аккаунт:</b> <code>{mafile.account_name}</code>\n"
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
            await callback.answer(f"Ошибка: {str(e)}", show_alert=True)
            logger.error(f"Ошибка обновления кода для {mafile.account_name}: {e}")
        finally:
            await client.close()

@dp.message(Command("login"))
@dp.message(Command("signin"))
@require_proxy
async def cmd_login(message: Message, command: CommandObject, **kwargs):  # добавить **kwargs
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
            await message.answer(f"❌ Аккаунт <b>{account_input}</b> не найден", parse_mode="HTML")
            return

        await perform_login_full(message, mafile, session)


@dp.message(Command("check_tokens"))
@dp.message(Command("tokens"))
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
            status_text += "<b>" + mf.account_name + "</b>:\n"

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

@dp.message(Command("delete_account"))
@dp.message(Command("delete"))
@dp.message(Command("remove"))
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
            await message.answer("❌ Аккаунт <b>" + account_input + "</b> не найден", parse_mode="HTML")
            return

        account_name = mafile.account_name
        await session.delete(mafile)
        await session.commit()

        await message.answer("✅ Аккаунт <b>" + account_name + "</b> удален", parse_mode="HTML")

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    """Отмена текущего действия"""
    user_id = message.from_user.id
    if user_id in user_states:
        state = user_states[user_id].get("state")
        user_states.pop(user_id)

        # Показываем разное сообщение в зависимости от того, что отменяем
        if state == "waiting_steam_credentials":
            await message.answer("❌ Добавление аккаунта отменено.\nИспользуйте /accounts для просмотра списка.")
        elif state == "waiting_bulk_credentials":
            await message.answer("❌ Массовый импорт отменен.")
        elif state == "waiting_note":
            await message.answer("❌ Изменение заметки отменено.")
        elif state == "waiting_unique_proxy":
            await message.answer("❌ Настройка прокси отменена.")
        else:
            await message.answer("❌ Действие отменено.")
    else:
        await message.answer("Нет активных действий для отмены.")
