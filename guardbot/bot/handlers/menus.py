"""Info, instruction and bot-settings menu screens."""

from aiogram import F, Router
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from guardbot.bot.runtime import callback_feedback
from guardbot.database import AsyncSessionLocal, User
from sqlalchemy import select

router = Router(name=__name__)


@router.callback_query(F.data == "info")
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
    await callback_feedback(callback)


@router.callback_query(F.data == "instruction")
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
    await callback_feedback(callback)


@router.callback_query(F.data == "settings")
async def callback_settings(callback: CallbackQuery):
    """Настройки бота"""
    await callback_feedback(callback)
    await update_settings_message(callback.message, callback.from_user.id)


async def update_settings_message(message: Message, telegram_id: int) -> None:
    """Render bot settings without acknowledging a callback twice."""

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

    await message.edit_text(settings_text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data == "mafile_instruction")
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
    await callback_feedback(callback)
