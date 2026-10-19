"""Trade menus and trade-notification settings handlers."""

import asyncio
import re
from html import escape

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from guardbot.bot.callback_validation import validated_callback_data
from guardbot.bot.runtime import (
    callback_feedback,
    check_proxy_available,
    clear_user_workflows,
    start_trade_check_task,
    stop_trade_check_task,
)
from guardbot.bot.states import TradeNotificationFlow
from guardbot.database import AsyncSessionLocal
from guardbot.services.ownership import get_owned_mafile

router = Router(name=__name__)


# ==================== НОВЫЕ ОБРАБОТЧИКИ ====================
@router.callback_query(F.data.startswith("trades_menu_"))
@validated_callback_data(r"trades_menu_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_trades_menu(callback: CallbackQuery):
    """Меню трейдов с новой клавиатурой"""
    mafile_id = int(callback.data.split("_")[2])
    telegram_id = callback.from_user.id

    has_proxy, _ = await check_proxy_available(telegram_id, for_steam_requests=True)
    if not has_proxy:
        await callback_feedback(callback, "❌ Нет доступного прокси!", show_alert=True)
        await callback.message.answer(
            "❌ <b>Нет доступного прокси!</b>\n\n"
            "Для просмотра трейдов необходим прокси.",
            parse_mode="HTML"
        )
        return

    # Получаем настройки уведомлений
    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return
        notifications_status = "🟢 ВКЛ" if mafile.trade_notifications else "🔴 ВЫКЛ"
        interval_text = f" (каждые {mafile.trade_notify_interval} мин)" if mafile.trade_notify_interval else ""

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📥 Входящие", callback_data=f"trades_incoming_active_{mafile_id}_1"),
                InlineKeyboardButton(text="📋 История входящих", callback_data=f"trades_incoming_history_{mafile_id}_1")
            ],
            [
                InlineKeyboardButton(text="📤 Исходящие", callback_data=f"trades_outgoing_active_{mafile_id}_1"),
                InlineKeyboardButton(text="📋 История исходящих", callback_data=f"trades_outgoing_history_{mafile_id}_1")
            ],
            [
                InlineKeyboardButton(text="🔗 Трейд-ссылка", callback_data=f"tradelink_{mafile_id}")
            ],
            [
                InlineKeyboardButton(
                    text=f"🔔 Уведомления о новых трейдах: {notifications_status}{interval_text}",
                    callback_data=f"trade_notifications_menu_{mafile_id}"
                )
            ],
            [
                InlineKeyboardButton(text="◀️ Назад", callback_data=f"account_detail_{mafile_id}")
            ]
        ]
    )

    await callback.message.edit_text(
        "🔄 <b>ТРЕЙДЫ</b>\n\n"
        "Выберите категорию:",
        parse_mode="HTML",
        reply_markup=keyboard
    )
    await callback_feedback(callback)

@router.callback_query(F.data.startswith("trade_notifications_menu_"))
@validated_callback_data(
    r"trade_notifications_menu_(?P<db_id>[1-9][0-9]{0,9})",
    clear_workflows_on_reject=True,
)
async def callback_trade_notifications_menu(
    callback: CallbackQuery,
    state: FSMContext,
):
    """Меню настройки уведомлений о трейдах"""
    await clear_user_workflows(callback.from_user.id, state)
    match = re.fullmatch(
        r"trade_notifications_menu_([1-9]\d*)",
        callback.data or "",
    )
    if match is None:
        await callback_feedback(callback, "❌ Некорректное действие")
        return
    mafile_id = int(match.group(1))

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        status_text = "🟢 Включены" if mafile.trade_notifications else "🔴 Выключены"
        interval_text = f"Интервал: каждые {mafile.trade_notify_interval} мин" if mafile.trade_notify_interval else "Интервал не задан"

        text = (
            f"🔔 <b>УВЕДОМЛЕНИЯ О ТРЕЙДАХ</b>\n\n"
            f"📱 Аккаунт: <code>{escape(mafile.account_name)}</code>\n"
            f"📊 Статус: {status_text}\n"
            f"⏱ {interval_text}\n\n"
            f"<i>При появлении новых трейдов бот отправит уведомление.</i>"
        )

        keyboard_buttons = []

        if mafile.trade_notifications:
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text="🔴 Выключить уведомления",
                    callback_data=f"trade_notify_toggle_{mafile_id}_off"
                )
            ])
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text="⏱ Изменить интервал",
                    callback_data=f"trade_notify_interval_{mafile_id}"
                )
            ])
        else:
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text="🟢 Включить уведомления",
                    callback_data=f"trade_notify_toggle_{mafile_id}_on"
                )
            ])

        keyboard_buttons.append([
            InlineKeyboardButton(
                text="◀️ Назад",
                callback_data=f"trades_menu_{mafile_id}"
            )
        ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        await callback_feedback(callback)


async def _start_trade_notification_interval_flow(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    mafile_id: int,
    account_name: str,
    is_update: bool,
    current_interval: int | None = None,
) -> None:
    if is_update:
        prompt = (
            "⏱ <b>Введите новый интервал проверки в минутах:</b>\n\n"
            f"Аккаунт: <code>{escape(account_name)}</code>\n"
            f"Текущий интервал: {current_interval} мин\n\n"
            "Отправьте число:"
        )
    else:
        prompt = (
            "⏱ <b>Введите интервал проверки в минутах:</b>\n\n"
            f"Аккаунт: <code>{escape(account_name)}</code>\n\n"
            "Рекомендуемое значение: 1-5 минут\n"
            "Минимум: 1 минута\n\n"
            "Отправьте число или нажмите отмену:"
        )

    try:
        await state.set_state(TradeNotificationFlow.waiting_interval)
        await state.set_data({"mafile_id": mafile_id, "is_update": is_update})
        await callback.message.edit_text(
            prompt,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=f"trade_notifications_menu_{mafile_id}",
                    )]
                ]
            ),
        )
    except (Exception, asyncio.CancelledError):
        await clear_user_workflows(callback.from_user.id, state)
        raise


@router.message(
    TradeNotificationFlow.waiting_interval,
    F.text,
    ~F.text.startswith("/"),
)
async def process_trade_notification_interval(
    message: Message,
    state: FSMContext,
):
    """Enable or update trade notifications from typed FSM input."""
    if message.chat.type != ChatType.PRIVATE:
        await message.answer(
            "❌ Интервал можно вводить только в личном чате с ботом."
        )
        return

    try:
        interval = int((message.text or "").strip())
    except ValueError:
        await message.answer("❌ Введите целое число от 1 до 60")
        return
    if not 1 <= interval <= 60:
        await message.answer("❌ Интервал должен быть от 1 до 60 минут")
        return

    state_data = await state.get_data()
    mafile_id = state_data.get("mafile_id")
    is_update = state_data.get("is_update")
    if (
        type(mafile_id) is not int
        or mafile_id <= 0
        or type(is_update) is not bool
    ):
        await clear_user_workflows(message.from_user.id, state)
        await message.answer("❌ Не удалось определить аккаунт")
        return

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, message.from_user.id)
        if not mafile:
            await clear_user_workflows(message.from_user.id, state)
            await message.answer("❌ Аккаунт не найден")
            return

        mafile.trade_notifications = True
        mafile.trade_notify_interval = interval
        mafile.known_trade_ids = []
        await session.commit()
        account_name = mafile.account_name

    await clear_user_workflows(message.from_user.id, state)
    start_trade_check_task(mafile_id)
    action = "обновлены" if is_update else "включены"

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="◀️ К настройкам уведомлений",
                callback_data=f"trade_notifications_menu_{mafile_id}",
            )]
        ]
    )
    await message.answer(
        f"✅ <b>Уведомления {action}!</b>\n\n"
        f"📱 Аккаунт: <code>{escape(account_name)}</code>\n"
        f"⏱ Интервал проверки: каждые {interval} мин\n\n"
        "Бот будет проверять новые трейды и присылать уведомления.",
        parse_mode="HTML",
        reply_markup=keyboard,
    )


# Обновите callback_trade_notify_toggle:
@router.callback_query(F.data.startswith("trade_notify_toggle_"))
@validated_callback_data(
    r"trade_notify_toggle_(?P<db_id>[1-9][0-9]{0,9})_(?:on|off)",
    clear_workflows_on_reject=True,
)
async def callback_trade_notify_toggle(
    callback: CallbackQuery,
    state: FSMContext,
):
    """Включение/выключение уведомлений"""
    await clear_user_workflows(callback.from_user.id, state)
    match = re.fullmatch(
        r"trade_notify_toggle_([1-9]\d*)_(on|off)",
        callback.data or "",
    )
    if match is None:
        await callback_feedback(callback, "❌ Некорректное действие")
        return
    mafile_id = int(match.group(1))
    action = match.group(2)

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        if action == "on":
            await _start_trade_notification_interval_flow(
                callback,
                state,
                mafile_id=mafile.id,
                account_name=mafile.account_name,
                is_update=False,
            )
            await callback_feedback(callback)

        else:  # off
            mafile.trade_notifications = False
            await session.commit()

            # Останавливаем задачу проверки
            stop_trade_check_task(mafile_id)

            await callback_feedback(callback, "✅ Уведомления выключены")
            callback.data = f"trade_notifications_menu_{mafile_id}"
            await callback_trade_notifications_menu(callback, state)


@router.callback_query(F.data.startswith("trade_notify_interval_"))
@validated_callback_data(
    r"trade_notify_interval_(?P<db_id>[1-9][0-9]{0,9})",
    clear_workflows_on_reject=True,
)
async def callback_trade_notify_interval(
    callback: CallbackQuery,
    state: FSMContext,
):
    """Изменение интервала уведомлений"""
    await clear_user_workflows(callback.from_user.id, state)
    match = re.fullmatch(
        r"trade_notify_interval_([1-9]\d*)",
        callback.data or "",
    )
    if match is None:
        await callback_feedback(callback, "❌ Некорректное действие")
        return
    mafile_id = int(match.group(1))

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        await _start_trade_notification_interval_flow(
            callback,
            state,
            mafile_id=mafile.id,
            account_name=mafile.account_name,
            is_update=True,
            current_interval=mafile.trade_notify_interval,
        )
        await callback_feedback(callback)
