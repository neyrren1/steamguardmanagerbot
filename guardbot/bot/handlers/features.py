"""Extracted from the legacy bot module without behavior changes."""

from html import escape
from urllib.parse import quote

from aiogram import F
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from guardbot.bot.callback_validation import validated_callback_data
from guardbot.bot.runtime import (
    bot,
    callback_feedback,
    calculate_steam_price,
    check_proxy_available,
    close_owned_resource,
    clear_legacy_user_state,
    clear_user_workflows,
    dp,
    get_item_price,
    get_inventory_context_id,
    process_inventory_items_universal,
    set_legacy_user_state,
    show_recovery_status,
    start_trade_check_task,
    stop_trade_check_task,
    user_states,
)
from guardbot.bot.states import BotPasswordFlow, TradeNotificationFlow, UnlockFlow
from guardbot.config import STEAM_CURRENCIES, logger
from guardbot.database import AsyncSessionLocal, Mafile, User
from guardbot.services.ownership import get_owned_mafile
from guardbot.services.session_manager import SteamSessionManager
from sqlalchemy import select, text
import asyncio
import re


_MAX_DATABASE_INTEGER = 2_147_483_647
_MAX_UNSIGNED_BIGINT = 18_446_744_073_709_551_615


def _bounded_identifier(value, maximum: int) -> str | None:
    if type(value) is int:
        numeric = value
    elif isinstance(value, str) and value and value.isascii() and value.isdecimal():
        numeric = int(value)
    else:
        return None
    if not 0 < numeric <= maximum:
        return None
    return str(numeric)


def _item_detail_callback(mafile_id, app_id, assetid) -> str | None:
    db_id = _bounded_identifier(mafile_id, _MAX_DATABASE_INTEGER)
    app = _bounded_identifier(app_id, _MAX_DATABASE_INTEGER)
    asset = _bounded_identifier(assetid, _MAX_UNSIGNED_BIGINT)
    if None in {db_id, app, asset}:
        return None
    payload = f"item_detail_{db_id}_{app}_{asset}"
    return payload if len(payload.encode("utf-8")) <= 64 else None


def _confirmation_callback(action: str, mafile_id, confirmation_id) -> str | None:
    if action not in {"confirm", "cancel"}:
        return None
    db_id = _bounded_identifier(mafile_id, _MAX_DATABASE_INTEGER)
    conf_id = _bounded_identifier(confirmation_id, _MAX_UNSIGNED_BIGINT)
    if None in {db_id, conf_id}:
        return None
    payload = f"{action}_single_{db_id}_{conf_id}"
    return payload if len(payload.encode("utf-8")) <= 64 else None


def _trade_accept_callback(mafile_id, tradeofferid) -> str | None:
    db_id = _bounded_identifier(mafile_id, _MAX_DATABASE_INTEGER)
    trade_id = _bounded_identifier(tradeofferid, _MAX_UNSIGNED_BIGINT)
    if None in {db_id, trade_id}:
        return None
    payload = f"trade_accept_{db_id}_{trade_id}"
    return payload if len(payload.encode("utf-8")) <= 64 else None


def _trade_detail_callback(mafile_id, tradeofferid, url_type: str) -> str | None:
    if url_type not in {"incoming", "outgoing"}:
        return None
    db_id = _bounded_identifier(mafile_id, _MAX_DATABASE_INTEGER)
    trade_id = _bounded_identifier(tradeofferid, _MAX_UNSIGNED_BIGINT)
    if None in {db_id, trade_id}:
        return None
    payload = f"trade_detail_full_{db_id}_{trade_id}_{url_type}"
    return payload if len(payload.encode("utf-8")) <= 64 else None


def _trade_decline_callback(mafile_id, tradeofferid) -> str | None:
    db_id = _bounded_identifier(mafile_id, _MAX_DATABASE_INTEGER)
    trade_id = _bounded_identifier(tradeofferid, _MAX_UNSIGNED_BIGINT)
    if None in {db_id, trade_id}:
        return None
    payload = f"trade_decline_{db_id}_{trade_id}"
    return payload if len(payload.encode("utf-8")) <= 64 else None


def _trade_cancel_callback(mafile_id, tradeofferid) -> str | None:
    db_id = _bounded_identifier(mafile_id, _MAX_DATABASE_INTEGER)
    trade_id = _bounded_identifier(tradeofferid, _MAX_UNSIGNED_BIGINT)
    if None in {db_id, trade_id}:
        return None
    payload = f"trade_cancel_{db_id}_{trade_id}"
    return payload if len(payload.encode("utf-8")) <= 64 else None


def _inventory_page_callback(
    mafile_id,
    app_id,
    context_id,
    page,
    cursor=None,
) -> str | None:
    db_id = _bounded_identifier(mafile_id, _MAX_DATABASE_INTEGER)
    app = _bounded_identifier(app_id, _MAX_DATABASE_INTEGER)
    context = _bounded_identifier(context_id, _MAX_DATABASE_INTEGER)
    page_value = _bounded_identifier(page, _MAX_DATABASE_INTEGER)
    if None in {db_id, app, context, page_value}:
        return None
    if (app, context) not in {
        ("730", "2"),
        ("570", "2"),
        ("440", "2"),
        ("753", "6"),
    }:
        return None

    suffix = ""
    if cursor == "prev":
        suffix = "_prev"
    elif cursor is not None:
        asset = _bounded_identifier(cursor, _MAX_UNSIGNED_BIGINT)
        if asset is None:
            return None
        suffix = f"_{asset}"

    payload = f"inventory_app_{db_id}_{app}_{context}_{page_value}{suffix}"
    return payload if len(payload.encode("utf-8")) <= 64 else None


# ==================== НОВЫЕ ОБРАБОТЧИКИ ====================
@dp.callback_query(F.data.startswith("trades_menu_"))
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

@dp.callback_query(F.data.startswith("trade_notifications_menu_"))
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


@dp.message(
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
@dp.callback_query(F.data.startswith("trade_notify_toggle_"))
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


@dp.callback_query(F.data.startswith("trade_notify_interval_"))
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

@dp.callback_query(F.data.startswith("tradelink_"))
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


# ---------- ИНВЕНТАРЬ ----------
@dp.callback_query(F.data.startswith("inventory_menu_"))
@validated_callback_data(
    r"inventory_menu_(?P<db_id>[1-9][0-9]{0,9})",
    clear_workflows_on_reject=True,
)
async def callback_inventory_menu(
    callback: CallbackQuery,
    state: FSMContext,
):
    """Меню инвентаря"""
    telegram_id = callback.from_user.id
    await clear_user_workflows(telegram_id, state)
    match = re.fullmatch(r"inventory_menu_([1-9]\d*)", callback.data or "")
    if match is None:
        await callback_feedback(callback, "❌ Некорректное действие")
        return
    mafile_id = int(match.group(1))

    # 🔥 Проверяем наличие прокси
    has_proxy, _ = await check_proxy_available(telegram_id, for_steam_requests=True)
    if not has_proxy:
        await callback_feedback(callback, "❌ Нет доступного прокси!", show_alert=True)
        await callback.message.answer(
            "❌ <b>Нет доступного прокси!</b>\n\n"
            "Для просмотра инвентаря необходим прокси.",
            parse_mode="HTML"
        )
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔫 CS2", callback_data=f"inventory_app_{mafile_id}_730_2_1")],
            [InlineKeyboardButton(text="🎮 Dota 2", callback_data=f"inventory_app_{mafile_id}_570_2_1")],
            [InlineKeyboardButton(text="🎯 TF2", callback_data=f"inventory_app_{mafile_id}_440_2_1")],
            [InlineKeyboardButton(text="📦 Steam", callback_data=f"inventory_app_{mafile_id}_753_6_1")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"account_detail_{mafile_id}")]
        ]
    )

    await callback.message.edit_text(
        "🎒 <b>ИНВЕНТАРЬ</b>\n\n"
        "Выберите игру:",
        parse_mode="HTML",
        reply_markup=keyboard
    )
    await callback_feedback(callback)



@dp.callback_query(F.data.startswith("inventory_app_"))
@validated_callback_data(
    r"inventory_app_(?P<db_id>[1-9][0-9]{0,9})_"
    r"(?P<int32_app>730|570|440|753)_"
    r"(?P<int32_context>2|6)_"
    r"(?P<int32_page>[1-9][0-9]{0,9})"
    r"(?:_(?:prev|(?P<uint64_asset>[1-9][0-9]{0,19})))?"
)
async def callback_inventory_app(callback: CallbackQuery):
    """Инвентарь конкретной игры с кнопками предметов"""
    match = re.fullmatch(
        r"inventory_app_([1-9][0-9]{0,9})_([1-9][0-9]{0,9})_"
        r"([1-9][0-9]{0,9})_([1-9][0-9]{0,9})(?:_(prev|[1-9][0-9]{0,19}))?",
        callback.data or "",
    )
    if match is None:
        await callback_feedback(callback, "❌ Некорректное действие")
        return
    mafile_id, app_id, context_id, page = map(int, match.groups()[:4])
    start_assetid = match.group(5)
    if (
        mafile_id > 2_147_483_647
        or app_id > 2_147_483_647
        or context_id > 2_147_483_647
        or page > 2_147_483_647
        or (
            start_assetid not in {None, "prev"}
            and int(start_assetid) > 18_446_744_073_709_551_615
        )
        or context_id != get_inventory_context_id(app_id)
    ):
        await callback_feedback(callback, "❌ Некорректное действие")
        return

    game_names = {730: "CS2", 570: "Dota 2", 440: "TF2", 753: "Steam"}
    game_name = game_names.get(app_id, f"App {app_id}")

    ITEMS_PER_PAGE = 10  # Уменьшаем до 10 для кнопок

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        if not mafile.steamid:
            await callback_feedback(callback, "SteamID не привязан", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            await callback.message.edit_text(
                f"🔄 <b>Загружаю инвентарь {game_name} (страница {page})...</b>",
                parse_mode="HTML"
            )

            data = await client.get_inventory_paginated(app_id, context_id, start_assetid, ITEMS_PER_PAGE)

            if not data:
                await callback.message.edit_text(
                    f"❌ <b>Не удалось загрузить инвентарь {game_name}</b>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[
                            InlineKeyboardButton(text="◀️ Назад", callback_data=f"inventory_menu_{mafile_id}")
                        ]]
                    )
                )
                return

            total_count = data.get('total_inventory_count', 0)

            if total_count == 0:
                await callback.message.edit_text(
                    f"📭 <b>Инвентарь {game_name} пуст</b>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[
                            InlineKeyboardButton(text="◀️ Назад", callback_data=f"inventory_menu_{mafile_id}")
                        ]]
                    )
                )
                return

            last_assetid = data.get('last_assetid')
            more_items = data.get('more_items', False)

            # Обрабатываем предметы универсальной функцией
            items = process_inventory_items_universal(data, app_id)

            # Заголовок
            tradable_on_page = sum(1 for i in items if i['tradable'])
            marketable_on_page = sum(1 for i in items if i['marketable'])

            text = f"🎒 <b>ИНВЕНТАРЬ {game_name}</b>\n"
            text += f"📦 Всего предметов: {total_count}\n"
            text += f"📄 Страница {page} | 🔄 {tradable_on_page} | 💰 {marketable_on_page}\n\n"
            text += "━━━━━━━━━━━━━━━━━━━━\n"
            text += "🔄 — обмен | 💰 — продажа | ❌ — недоступно\n"
            text += "🔴 Covert | 🟣 Classified | 🔵 Restricted | ⚪️ Common"

            # Кнопки предметов
            keyboard_buttons = []

            for idx, item in enumerate(items, 1):
                global_idx = (page - 1) * ITEMS_PER_PAGE + idx

                tradable_icon = "🔄" if item['tradable'] else "❌"
                marketable_icon = "💰" if item['marketable'] else "❌"

                # Обрезаем название для кнопки
                name_display = item['name']
                if len(name_display) > 40:
                    name_display = name_display[:37] + "..."

                button_text = f"{global_idx}. {tradable_icon} {marketable_icon} {item['rarity_emoji']} {name_display}"
                if item['amount'] > 1:
                    button_text += f" x{item['amount']}"

                detail_callback = _item_detail_callback(
                    mafile_id,
                    app_id,
                    item.get("assetid"),
                )
                if detail_callback is not None:
                    keyboard_buttons.append([InlineKeyboardButton(
                        text=button_text,
                        callback_data=detail_callback,
                    )])

            # Навигация
            nav_buttons = []

            if page > 1:
                previous_callback = _inventory_page_callback(
                    mafile_id, app_id, context_id, page - 1, "prev"
                )
                if previous_callback is not None:
                    nav_buttons.append(InlineKeyboardButton(
                        text="◀️ Назад",
                        callback_data=previous_callback,
                    ))

            nav_buttons.append(InlineKeyboardButton(
                text=f"📄 {page}",
                callback_data="page_info"
            ))

            if more_items and last_assetid:
                next_callback = _inventory_page_callback(
                    mafile_id, app_id, context_id, page + 1, last_assetid
                )
                if next_callback is not None:
                    nav_buttons.append(InlineKeyboardButton(
                        text="Вперед ▶️",
                        callback_data=next_callback,
                    ))

            if nav_buttons:
                keyboard_buttons.append(nav_buttons)

            # Дополнительные кнопки
            keyboard_buttons.append([
                InlineKeyboardButton(text="🔄 Обновить", callback_data=f"inventory_app_{mafile_id}_{app_id}_{context_id}_1")
            ])
            keyboard_buttons.append([
                InlineKeyboardButton(text="◀️ В меню инвентаря", callback_data=f"inventory_menu_{mafile_id}")
            ])

            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
            )

        except Exception as e:
            logger.error(f"Inventory error: {e}", exc_info=True)
            await callback.message.edit_text(
                f"❌ Ошибка при загрузке инвентаря\n\n{str(e)[:100]}",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(text="◀️ Назад", callback_data=f"inventory_menu_{mafile_id}")
                    ]]
                )
            )
        finally:
            await client.close()
            await callback_feedback(callback)

@dp.callback_query(F.data.startswith("item_detail_"))
@validated_callback_data(
    r"item_detail_(?P<db_id>[1-9][0-9]{0,9})_"
    r"(?P<int32_app>[1-9][0-9]{0,9})_"
    r"(?P<uint64_asset>[1-9][0-9]{0,19})"
)
async def callback_item_detail(callback: CallbackQuery):
    """Детальный просмотр предмета инвентаря"""
    parts = callback.data.split("_")
    mafile_id = int(parts[2])
    app_id = int(parts[3])
    assetid = parts[4]

    game_names = {730: "CS2", 570: "Dota 2", 440: "TF2", 753: "Steam"}
    game_name = game_names.get(app_id, f"App {app_id}")

    await callback_feedback(callback, "Загружаю детали предмета...")

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            context_id = get_inventory_context_id(app_id)
            data = await client.get_inventory(app_id, context_id)

            if not data:
                await callback.message.edit_text("❌ Не удалось загрузить данные предмета", parse_mode="HTML")
                return

            items = process_inventory_items_universal(data, app_id)
            item = next((i for i in items if i['assetid'] == assetid), None)

            if not item:
                await callback.message.edit_text("❌ Предмет не найден", parse_mode="HTML")
                return

            # 🔥 Получаем валюту из БД, если нет — определяем
            country = mafile.country
            currency = mafile.currency_id

            # Если страна или валюта не определены в БД — пытаемся определить сейчас
            if not country or not currency:
                logger.info(f"🌍 [{mafile.account_name}] Country/currency not in DB, detecting...")
                try:
                    detected_country = await client.get_account_country()
                    if detected_country:
                        country = detected_country
                        mafile.country = country
                        # Определяем currency_id по стране
                        for curr_id, info in STEAM_CURRENCIES.items():
                            if info['country'] == country:
                                currency = curr_id
                                mafile.currency_id = currency
                                break
                        await session.commit()
                        logger.info(f"✅ [{mafile.account_name}] Detected: country={country}, currency={currency}")
                except Exception as e:
                    logger.warning(f"⚠️ [{mafile.account_name}] Failed to detect country: {e}")

            # Если всё ещё нет — fallback на USD
            if not country:
                country = "US"
            if not currency:
                currency = 1

            # 🔥 Получаем цену предмета в правильной валюте
            price_data = None
            if item.get('marketable') and item.get('market_hash_name'):
                await client._ensure_session()
                price_data = await get_item_price(client, app_id, item['market_hash_name'], country, currency)

            # Формируем текст
            text = f"🎒 <b>ДЕТАЛИ ПРЕДМЕТА</b>\n\n"
            text += f"📛 <b>Название:</b> {escape(str(item['name']))}\n"

            # Name Tag
            if item.get('name_tag'):
                text += f"🏷️ <b>Name Tag:</b> <code>{escape(str(item['name_tag']))}</code>\n"

            text += f"📦 <b>Тип:</b> {escape(str(item['item_type']))}\n"
            text += f"{escape(str(item['rarity_emoji']))} <b>Редкость:</b> {escape(str(item['rarity']))}\n"

            if item['exterior']:
                text += f"🎨 <b>Износ:</b> {escape(str(item['exterior']))}\n"

            if item['collection']:
                text += f"📚 <b>Коллекция:</b> {escape(str(item['collection']))}\n"

            if item['hero']:
                text += f"👤 <b>Герой:</b> {escape(str(item['hero']))}\n"

            if item['quality']:
                text += f"⭐ <b>Качество:</b> {escape(str(item['quality']))}\n"

            # Float
            if item['float'] is not None:
                text += f"\n🔬 <b>Float:</b> <code>{item['float']:.15f}</code>\n"

            # Pattern
            if item['pattern'] is not None:
                text += f"🎯 <b>Pattern:</b> <code>{escape(str(item['pattern']))}</code>\n"

            # Paint Seed
            if item['paint_seed'] is not None:
                text += f"🌱 <b>Paint Seed:</b> <code>{escape(str(item['paint_seed']))}</code>\n"

            # 🔥 ЦЕНЫ (в валюте аккаунта)
            if price_data:
                currency_symbol = STEAM_CURRENCIES.get(currency, {}).get('symbol', '')

                # Парсим минимальную цену
                import re
                min_match = re.search(r'[\d.,]+', price_data['lowest_price'].replace(' ', ''))
                if min_match:
                    min_float = float(min_match.group().replace(',', '.'))
                    min_after = calculate_steam_price(min_float, currency)
                    min_after_str = f"{min_after/100:.2f}{currency_symbol}"
                else:
                    min_after_str = "N/A"

                # Парсим среднюю цену
                med_match = re.search(r'[\d.,]+', price_data['median_price'].replace(' ', ''))
                if med_match:
                    med_float = float(med_match.group().replace(',', '.'))
                    med_after = calculate_steam_price(med_float, currency)
                    med_after_str = f"{med_after/100:.2f}{currency_symbol}"
                else:
                    med_after_str = "N/A"

                text += f"\n💰 <b>Первая цена Steam:</b> {escape(str(price_data['lowest_price']))} <i>(после комиссии: {escape(min_after_str)})</i>\n"
                text += f"📊 <b>Средняя цена Steam:</b> {escape(str(price_data['median_price']))} <i>(после комиссии: {escape(med_after_str)})</i>\n"
                text += f"📈 <b>Продаж за 24ч:</b> {escape(str(price_data['volume']))} шт.\n"

            # StatTrak
            if item['is_stattrak']:
                text += f"\n📊 <b>StatTrak™:</b> "
                if item['stattrak_count'] is not None:
                    text += f"<code>{escape(str(item['stattrak_count']))}</code> убийств\n"
                else:
                    text += "да\n"

            # Обмен/Продажа
            text += f"\n🔄 <b>Обмен:</b> {'✅ Да' if item['tradable'] else '❌ Нет'}\n"
            text += f"💰 <b>Продажа:</b> {'✅ Да' if item['marketable'] else '❌ Нет'}\n"

            if item['amount'] > 1:
                text += f"📦 <b>Количество:</b> {escape(str(item['amount']))}\n"

            # ССЫЛКА НА ОСМОТР
            if item.get('inspect_link'):
                text += f"\n🔍 <b>Осмотр в игре:</b>\n<code>{escape(str(item['inspect_link']))}</code>\n"

            # НАКЛЕЙКИ С ИЗНОСОМ
            if item['stickers']:
                text += f"\n<b>🏷️ НАКЛЕЙКИ ({len(item['stickers'])}):</b>\n"
                for i, sticker in enumerate(item['stickers'], 1):
                    if sticker['wear'] is not None and sticker['wear'] > 0:
                        wear_percent = round(sticker['wear'] * 100)
                        text += f"  {i}. {escape(str(sticker['name']))} ({wear_percent}%)\n"
                    else:
                        text += f"  {i}. {escape(str(sticker['name']))}\n"

            # Кнопки
            keyboard_buttons = []

            # 🔥 КНОПКИ ПРОДАЖИ (если предмет можно продавать)
            if item.get('marketable') and item.get('market_hash_name') and price_data:
                currency_symbol = STEAM_CURRENCIES.get(currency, {}).get('symbol', '')

                # Парсим цены для кнопок
                import re
                min_match = re.search(r'[\d.,]+', price_data['lowest_price'].replace(' ', ''))
                min_float = float(min_match.group().replace(',', '.')) if min_match else 0
                min_after = calculate_steam_price(min_float, currency)
                min_after_str = f"{min_after/100:.2f}{currency_symbol}"

                med_match = re.search(r'[\d.,]+', price_data['median_price'].replace(' ', ''))
                med_float = float(med_match.group().replace(',', '.')) if med_match else 0
                med_after = calculate_steam_price(med_float, currency)
                med_after_str = f"{med_after/100:.2f}{currency_symbol}"

                keyboard_buttons.append([
                    InlineKeyboardButton(
                        text=f"💰 Продать за {price_data['lowest_price']} ({min_after_str})",
                        callback_data=f"sell_item_min_{mafile_id}_{app_id}_{assetid}"
                    )
                ])
                keyboard_buttons.append([
                    InlineKeyboardButton(
                        text=f"📊 Продать за {price_data['median_price']} ({med_after_str})",
                        callback_data=f"sell_item_median_{mafile_id}_{app_id}_{assetid}"
                    )
                ])
                keyboard_buttons.append([
                    InlineKeyboardButton(
                        text="✏️ Продать по своей цене",
                        callback_data=f"sell_item_custom_{mafile_id}_{app_id}_{assetid}"
                    )
                ])

            if item['market_hash_name']:
                market_url = f"https://steamcommunity.com/market/listings/{app_id}/{quote(str(item['market_hash_name']), safe='')}"
                keyboard_buttons.append([
                    InlineKeyboardButton(text="💹 Открыть на Steam Market", url=market_url)
                ])

            keyboard_buttons.append([
                InlineKeyboardButton(
                    text="◀️ Назад к инвентарю",
                    callback_data=(
                        f"inventory_app_{mafile_id}_{app_id}_{context_id}_1"
                    )
                )
            ])
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text="🏠 В меню инвентаря",
                    callback_data=f"inventory_menu_{mafile_id}"
                )
            ])

            await callback.message.edit_text(
                text,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
            )

        except Exception as e:
            logger.error(f"Error loading item detail: {e}", exc_info=True)
            await callback.message.edit_text(
                f"❌ Ошибка при загрузке предмета\n\n{escape(str(e)[:200])}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(text="◀️ Назад", callback_data=f"inventory_menu_{mafile_id}")
                    ]]
                )
            )
        finally:
            await close_owned_resource(client)

def process_inventory_items_simple(data: dict) -> list:
    """Обработка предметов инвентаря (упрощённая версия)"""
    assets = data.get('assets', [])
    descriptions = data.get('descriptions', [])

    # Словарь описаний
    desc_dict = {}
    for desc in descriptions:
        key = f"{desc.get('classid')}_{desc.get('instanceid')}"
        desc_dict[key] = desc

    # Карта названий редкости -> эмодзи
    rarity_to_emoji = {
        'Covert': '🔴',
        'Extraordinary': '🔴',
        'Classified': '🟣',
        'Restricted': '🟣',
        'Remarkable': '🟣',
        'Mil-Spec Grade': '🔵',
        'High Grade': '🔵',
        'Industrial Grade': '🔵',
        'Consumer Grade': '⚪',
        'Base Grade': '⚪',
        'Stock': '⚪',
    }

    items = []
    for asset in assets:
        classid = asset.get('classid')
        instanceid = asset.get('instanceid')
        amount = int(asset.get('amount', 1))
        key = f"{classid}_{instanceid}"
        desc = desc_dict.get(key, {})

        # Редкость и эмодзи
        rarity_emoji = '⚪'

        for tag in desc.get('tags', []):
            if tag.get('category') == 'Rarity':
                rarity_name = tag.get('localized_tag_name', '')
                rarity_emoji = rarity_to_emoji.get(rarity_name, '⚪')
                break

        name = desc.get('market_name') or desc.get('name') or 'Unknown Item'

        items.append({
            'name': name,
            'rarity_emoji': rarity_emoji,
            'tradable': desc.get('tradable', 0) == 1,
            'marketable': desc.get('marketable', 0) == 1,
            'amount': amount
        })

    return items


def process_inventory_items(data: dict) -> list:
    """Обработка предметов инвентаря"""
    assets = data.get('assets', [])
    descriptions = data.get('descriptions', [])

    # Словарь описаний
    desc_dict = {}
    for desc in descriptions:
        key = f"{desc.get('classid')}_{desc.get('instanceid')}"
        desc_dict[key] = desc

    items = []
    for asset in assets:
        classid = asset.get('classid')
        instanceid = asset.get('instanceid')
        assetid = asset.get('assetid')
        amount = int(asset.get('amount', 1))
        key = f"{classid}_{instanceid}"
        desc = desc_dict.get(key, {})

        # Редкость и цвет
        rarity = ''
        rarity_color = '#b0c3d9'
        exterior = ''

        for tag in desc.get('tags', []):
            category = tag.get('category', '')
            if category == 'Rarity':
                rarity = tag.get('localized_tag_name', '')
                color = tag.get('color', '')
                if color:
                    rarity_color = f"#{color}" if not color.startswith('#') else color
            elif category == 'Exterior':
                exterior = tag.get('localized_tag_name', '')

        # Float
        float_val = None
        paintwear = desc.get('paintwear')
        if paintwear is not None:
            try:
                float_val = float(paintwear)
            except:
                pass

        # Pattern
        pattern = None
        for action in desc.get('actions', []):
            if action.get('name') == 'Inspect in Game...':
                link = action.get('link', '')
                if 'p' in link:
                    try:
                        import re
                        match = re.search(r'[?&]p=(\d+)', link)
                        if match:
                            pattern = int(match.group(1))
                    except:
                        pass
                break

        items.append({
            'assetid': assetid,
            'name': desc.get('market_name') or desc.get('name') or 'Unknown Item',
            'rarity': rarity,
            'rarity_color': rarity_color,
            'exterior': exterior,
            'tradable': desc.get('tradable', 0) == 1,
            'marketable': desc.get('marketable', 0) == 1,
            'float': float_val,
            'pattern': pattern,
            'amount': amount
        })

    return items

@dp.callback_query(F.data.startswith("confirmations_menu_"))
@validated_callback_data(r"confirmations_menu_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_confirmations_menu(callback: CallbackQuery, send_new: bool = False):
    """Просмотр подтверждений"""
    mafile_id = int(callback.data.split("_")[2])
    telegram_id = callback.from_user.id

    logger.info(f"Opening confirmations menu for mafile_id={mafile_id}, user={telegram_id}")

    # Проверяем наличие прокси
    has_proxy, _ = await check_proxy_available(telegram_id, for_steam_requests=True)
    if not has_proxy:
        await callback_feedback(callback, "❌ Нет доступного прокси!", show_alert=True)
        await callback.message.answer(
            "❌ <b>Нет доступного прокси!</b>\n\n"
            "Для работы с подтверждениями необходим прокси.",
            parse_mode="HTML"
        )
        return

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)

        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        if not mafile.identity_secret:
            await callback_feedback(callback, "Нет identity_secret для подтверждений", show_alert=True)
            return

        if not mafile.access_token:
            await callback_feedback(callback, "Нет активной сессии. Сначала войдите в аккаунт", show_alert=True)
            await callback.message.edit_text(
                f"❌ <b>Нет активной сессии!</b>\n\n"
                f"Сначала войдите в аккаунт:\n"
                f"<code>/login {escape(mafile.account_name)}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(text="🔑 Войти по паролю", callback_data=f"action_login_{mafile_id}"),
                            InlineKeyboardButton(text="🔄 Войти по токену", callback_data=f"try_refresh_{mafile_id}")
                        ],
                        [InlineKeyboardButton(
                            text="◀️ Назад",
                            callback_data=f"account_detail_{mafile_id}"
                        )]
                    ]
                )
            )
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            # 🔥 Если send_new=True, отправляем новое сообщение вместо редактирования
            if send_new:
                await callback.message.delete()
                status_msg = await callback.message.answer(
                    "🔄 <b>Загружаю подтверждения...</b>",
                    parse_mode="HTML"
                )
            else:
                try:
                    status_msg = await callback.message.edit_text(
                        "🔄 <b>Загружаю подтверждения...</b>",
                        parse_mode="HTML"
                    )
                except:
                    status_msg = callback.message

            data = await client.get_trade_confirmations()

            # Показываем всплывашки о статусе восстановления
            await show_recovery_status(callback, client)

            if data and data.get('success'):
                confirmations = data.get('conf', [])

                if not confirmations:
                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="🔄 Обновить",
                                callback_data=f"confirmations_menu_{mafile_id}"
                            )],
                            [InlineKeyboardButton(
                                text="◀️ Назад",
                                callback_data=f"account_detail_{mafile_id}"
                            )]
                        ]
                    )

                    await status_msg.edit_text(
                        "✅ <b>Нет ожидающих подтверждений</b>",
                        parse_mode="HTML",
                        reply_markup=keyboard
                    )
                    return

                # 🔥 Подсчитываем типы подтверждений
                trade_count = sum(1 for c in confirmations if c.get('type') == 2)
                sell_count = sum(1 for c in confirmations if c.get('type') == 3)
                other_count = len(confirmations) - trade_count - sell_count

                # 🔥 Заголовок
                text = f"📋 <b>ПОДТВЕРЖДЕНИЯ ({len(confirmations)})</b>\n"
                if trade_count > 0:
                    text += f"├─ 🔄 Трейды: {trade_count}\n"
                if sell_count > 0:
                    text += f"├─ 💰 Продажи: {sell_count}\n"
                if other_count > 0:
                    text += f"├─ ❓ Другое: {other_count}\n"
                text += "\n"

                # 🔥 Кнопки для каждого подтверждения
                keyboard_buttons = []

                for i, conf in enumerate(confirmations[:15], 1):
                    conf_id = conf.get('id')
                    conf_type = conf.get('type')
                    headline = escape(str(conf.get('headline', '')))
                    summary = [
                        escape(str(value))
                        for value in conf.get('summary', [])
                    ]
                    type_name = escape(str(conf.get('type_name', '')))

                    # 🔥 Форматируем в зависимости от типа
                    if conf_type == 3:  # Продажа
                        item_name = summary[0] if summary else 'Предмет'
                        price_text = headline.replace('Selling for', '').strip()
                        display_text = f"💰 {item_name} → {price_text}"
                        btn_label = f"💰 {i}"

                    elif conf_type == 2:  # Трейд
                        partner_name = headline if headline else 'Неизвестный'
                        display_text = f"🔄 Трейд с {partner_name}"

                        if summary:
                            display_text += "\n"
                            for line in summary[:3]:
                                line_clean = line.strip()
                                if 'give up' in line_clean.lower() or 'отдадите' in line_clean.lower():
                                    display_text += f"   📤 {line_clean}\n"
                                elif 'for their' in line_clean.lower() or 'за их' in line_clean.lower():
                                    display_text += f"   📥 {line_clean}\n"
                                else:
                                    display_text += f"   • {line_clean}\n"
                            display_text = display_text.rstrip('\n')

                        btn_label = f"🔄 {i}"

                    else:  # Другое
                        display_text = f"❓ {type_name}: {headline}"
                        if summary:
                            display_text += f" ({', '.join(summary[:2])})"
                        btn_label = f"❓ {i}"

                    text += f"{i}. {display_text}\n"

                    confirm_callback = _confirmation_callback(
                        "confirm",
                        mafile_id,
                        conf_id,
                    )
                    cancel_callback = _confirmation_callback(
                        "cancel",
                        mafile_id,
                        conf_id,
                    )
                    if confirm_callback and cancel_callback:
                        keyboard_buttons.append([
                            InlineKeyboardButton(
                                text=f"✅ {btn_label}",
                                callback_data=confirm_callback,
                            ),
                            InlineKeyboardButton(
                                text=f"❌ {btn_label}",
                                callback_data=cancel_callback,
                            )
                        ])

                if len(confirmations) > 15:
                    text += f"\n<i>... и еще {len(confirmations) - 15}</i>"

                text += "\n<i>Используйте кнопки для подтверждения по одному</i>"

                keyboard_buttons.append([
                    InlineKeyboardButton(
                        text=f"✅ Подтвердить ВСЕ ({len(confirmations)})",
                        callback_data=f"confirm_all_{mafile_id}"
                    )
                ])
                keyboard_buttons.append([
                    InlineKeyboardButton(
                        text=f"❌ Отменить ВСЕ ({len(confirmations)})",
                        callback_data=f"cancel_all_{mafile_id}"
                    )
                ])
                keyboard_buttons.append([
                    InlineKeyboardButton(
                        text="🔄 Обновить",
                        callback_data=f"confirmations_menu_{mafile_id}"
                    ),
                    InlineKeyboardButton(
                        text="◀️ Назад",
                        callback_data=f"account_detail_{mafile_id}"
                    )
                ])

                keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

                await status_msg.edit_text(
                    text,
                    parse_mode="HTML",
                    reply_markup=keyboard
                )

            elif data and data.get('needauth'):
                await status_msg.edit_text(
                    f"❌ <b>Не удалось восстановить сессию!</b>\n\n"
                    f"Требуется повторный вход в аккаунт.\n\n"
                    f"<i>Выберите действие:</i>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(text="🔑 Войти по паролю", callback_data=f"action_login_{mafile_id}"),
                                InlineKeyboardButton(text="🔄 Войти по токену", callback_data=f"try_refresh_{mafile_id}")
                            ],
                            [InlineKeyboardButton(
                                text="🔄 Попробовать снова",
                                callback_data=f"confirmations_menu_{mafile_id}"
                            )],
                            [InlineKeyboardButton(
                                text="◀️ Назад",
                                callback_data=f"account_detail_{mafile_id}"
                            )]
                        ]
                    )
                )

            else:
                error_text = data.get('message', 'Неизвестная ошибка') if data else 'Нет ответа от Steam'

                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="🔄 Повторить",
                            callback_data=f"confirmations_menu_{mafile_id}"
                        )],
                        [
                            InlineKeyboardButton(text="🔑 Войти по паролю", callback_data=f"action_login_{mafile_id}"),
                            InlineKeyboardButton(text="🔄 Войти по токену", callback_data=f"try_refresh_{mafile_id}")
                        ],
                        [InlineKeyboardButton(
                            text="◀️ Назад",
                            callback_data=f"account_detail_{mafile_id}"
                        )]
                    ]
                )

                await status_msg.edit_text(
                    f"❌ <b>Не удалось загрузить подтверждения</b>\n\n"
                    f"<code>{escape(str(error_text)[:100])}</code>",
                    parse_mode="HTML",
                    reply_markup=keyboard
                )

        except Exception as e:
            logger.error(f"Error loading confirmations for {mafile.account_name}: {e}")

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="🔄 Повторить",
                        callback_data=f"confirmations_menu_{mafile_id}"
                    )],
                    [
                        InlineKeyboardButton(text="🔑 Войти по паролю", callback_data=f"action_login_{mafile_id}"),
                        InlineKeyboardButton(text="🔄 Войти по токену", callback_data=f"try_refresh_{mafile_id}")
                    ],
                    [InlineKeyboardButton(
                        text="◀️ Назад",
                        callback_data=f"account_detail_{mafile_id}"
                    )]
                ]
            )

            await callback.message.edit_text(
                f"❌ <b>Ошибка при загрузке подтверждений</b>\n\n"
                f"<code>{escape(str(e)[:100])}</code>",
                parse_mode="HTML",
                reply_markup=keyboard
            )

        finally:
            await client.close()

# 🔥 Обработчики для подтверждения/отмены по одному

@dp.callback_query(F.data.startswith("confirm_single_"))
@validated_callback_data(
    r"confirm_single_(?P<db_id>[1-9][0-9]{0,9})_"
    r"(?P<uint64_conf>[1-9][0-9]{0,19})"
)
async def callback_confirm_single(callback: CallbackQuery):
    parts = callback.data.split("_")
    mafile_id = int(parts[2])
    conf_id = parts[3]

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            await callback_feedback(callback, "Подтверждаю...")

            # 🔥 ДЕЛАЕМ КАК В confirm_all - получаем свежий список
            data = await client.get_trade_confirmations()

            if not data or not data.get('success'):
                await callback_feedback(callback, "❌ Не удалось загрузить подтверждения", show_alert=True)
                return

            # Находим нужное подтверждение
            target_conf = None
            for conf in data.get('conf', []):
                if str(conf.get('id')) == conf_id:
                    target_conf = conf
                    break

            if not target_conf:
                await callback_feedback(callback, "❌ Подтверждение не найдено", show_alert=True)
                return

            # Используем СВЕЖИЙ conf_key из списка
            fresh_conf_id = target_conf.get('id')
            fresh_conf_key = target_conf.get('nonce')

            # Отправляем подтверждение
            result = await client.send_confirmation(fresh_conf_id, fresh_conf_key, allow=True)

            if result:
                await callback_feedback(callback, "✅ Подтверждено!", show_alert=False)
                await SteamSessionManager.save_steam_session_to_db(session, mafile.id, client)
            else:
                await callback_feedback(callback, "❌ Ошибка подтверждения", show_alert=True)
                return

            callback.data = f"confirmations_menu_{mafile_id}"
            await callback_confirmations_menu(callback, send_new=True)

        except Exception as e:
            logger.error(f"Error confirming single: {e}", exc_info=True)
            await callback_feedback(callback, f"Ошибка: {str(e)[:50]}", show_alert=True)
        finally:
            await client.close()

@dp.callback_query(F.data.startswith("cancel_single_"))
@validated_callback_data(
    r"cancel_single_(?P<db_id>[1-9][0-9]{0,9})_"
    r"(?P<uint64_conf>[1-9][0-9]{0,19})"
)
async def callback_cancel_single(callback: CallbackQuery):
    """Отмена одного действия - РАБОТАЕТ КАК confirm_all"""
    parts = callback.data.split("_")
    mafile_id = int(parts[2])
    conf_id = parts[3]

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            await callback_feedback(callback, "Отменяю...")

            data = await client.get_trade_confirmations()
            if not data or not data.get("success"):
                await callback_feedback(
                    callback,
                    "❌ Не удалось загрузить подтверждения",
                    show_alert=True,
                )
                return

            target_conf = next(
                (
                    conf
                    for conf in data.get("conf", [])
                    if str(conf.get("id")) == conf_id
                ),
                None,
            )
            if target_conf is None:
                await callback_feedback(
                    callback,
                    "❌ Подтверждение не найдено",
                    show_alert=True,
                )
                return

            result = await client.send_confirmation(
                target_conf.get("id"),
                target_conf.get("nonce"),
                allow=False,
            )

            if result:
                await callback_feedback(callback, "✅ Отменено!", show_alert=False)
                await SteamSessionManager.save_steam_session_to_db(session, mafile.id, client)
            else:
                await callback_feedback(callback, "❌ Ошибка отмены", show_alert=True)
                return

            callback.data = f"confirmations_menu_{mafile_id}"
            await callback_confirmations_menu(callback, send_new=True)

        except Exception as e:
            logger.error(f"Error canceling single: {e}", exc_info=True)
            await callback_feedback(callback, f"Ошибка: {str(e)[:50]}", show_alert=True)
        finally:
            await client.close()

@dp.callback_query(F.data.startswith("confirm_all_"))
@validated_callback_data(r"confirm_all_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_confirm_all(callback: CallbackQuery):
    """Подтвердить все ожидающие действия"""
    mafile_id = int(callback.data.split("_")[2])

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)

        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            await callback_feedback(callback, "Начинаю подтверждение...")

            status_msg = await callback.message.edit_text(
                "🔄 <b>Подтверждаю все действия...</b>",
                parse_mode="HTML"
            )

            data = await client.get_trade_confirmations()

            if not data or not data.get('success'):
                await status_msg.edit_text(
                    "❌ Не удалось загрузить подтверждения",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[
                            InlineKeyboardButton(text="◀️ Назад", callback_data=f"confirmations_menu_{mafile_id}")
                        ]]
                    )
                )
                return

            confirmations = data.get('conf', [])

            if not confirmations:
                await status_msg.edit_text(
                    "✅ Нет ожидающих подтверждений",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[
                            InlineKeyboardButton(text="◀️ Назад", callback_data=f"confirmations_menu_{mafile_id}")
                        ]]
                    )
                )
                return

            success_count = 0
            fail_count = 0

            for conf in confirmations:
                conf_id = conf.get('id')
                conf_key = conf.get('nonce')

                if conf_id and conf_key:
                    result = await client.send_confirmation(conf_id, conf_key, allow=True)
                    if result:
                        success_count += 1
                    else:
                        fail_count += 1

                await asyncio.sleep(0.5)  # Пауза между запросами

            await status_msg.edit_text(
                f"📊 <b>Результат подтверждения:</b>\n\n"
                f"✅ Успешно: {success_count}\n"
                f"❌ Ошибок: {fail_count}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="🔄 Обновить список", callback_data=f"confirmations_menu_{mafile_id}")],
                        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"account_detail_{mafile_id}")]
                    ]
                )
            )

        except Exception as e:
            logger.error(f"Error confirming all for {mafile.account_name}: {e}")

            await status_msg.edit_text(
                f"❌ Ошибка: {escape(str(e)[:100])}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(text="◀️ Назад", callback_data=f"confirmations_menu_{mafile_id}")
                    ]]
                )
            )

        finally:
            await client.close()


@dp.callback_query(F.data.startswith("cancel_all_"))
@validated_callback_data(r"cancel_all_(?P<db_id>[1-9][0-9]{0,9})")
async def callback_cancel_all(callback: CallbackQuery):
    """Отменить все ожидающие действия"""
    mafile_id = int(callback.data.split("_")[2])

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)

        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            await callback_feedback(callback, "Отменяю все действия...")

            status_msg = await callback.message.edit_text(
                "🔄 <b>Отменяю все действия...</b>",
                parse_mode="HTML"
            )

            data = await client.get_trade_confirmations()

            if not data or not data.get('success'):
                await status_msg.edit_text(
                    "❌ Не удалось загрузить подтверждения",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[
                            InlineKeyboardButton(text="◀️ Назад", callback_data=f"confirmations_menu_{mafile_id}")
                        ]]
                    )
                )
                return

            confirmations = data.get('conf', [])

            if not confirmations:
                await status_msg.edit_text(
                    "✅ Нет ожидающих подтверждений",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[
                            InlineKeyboardButton(text="◀️ Назад", callback_data=f"confirmations_menu_{mafile_id}")
                        ]]
                    )
                )
                return

            success_count = 0
            fail_count = 0

            for conf in confirmations:
                conf_id = conf.get('id')
                conf_key = conf.get('nonce')

                if conf_id and conf_key:
                    result = await client.send_confirmation(conf_id, conf_key, allow=False)
                    if result:
                        success_count += 1
                    else:
                        fail_count += 1

                await asyncio.sleep(0.5)

            await status_msg.edit_text(
                f"📊 <b>Результат отмены:</b>\n\n"
                f"✅ Успешно: {success_count}\n"
                f"❌ Ошибок: {fail_count}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="🔄 Обновить список", callback_data=f"confirmations_menu_{mafile_id}")],
                        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"account_detail_{mafile_id}")]
                    ]
                )
            )

        except Exception as e:
            logger.error(f"Error canceling all for {mafile.account_name}: {e}")

            await status_msg.edit_text(
                f"❌ Ошибка: {escape(str(e)[:100])}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(text="◀️ Назад", callback_data=f"confirmations_menu_{mafile_id}")
                    ]]
                )
            )

        finally:
            await client.close()

# ==================== ЗАМЕНИТЬ ОБРАБОТЧИКИ ТРЕЙДОВ ====================

async def fetch_and_display_trades(callback: CallbackQuery, mafile_id: int, client,
                                   trade_type: str, page: int = 1):
    """Универсальная функция для отображения трейдов"""

    ITEMS_PER_PAGE = 10

    # Определяем параметры в зависимости от типа
    if trade_type == "incoming_active":
        url_type = "incoming"
        history = False
        filter_status = ['active']
        title = "📥 ВХОДЯЩИЕ ТРЕЙДЫ (АКТИВНЫЕ)"

    elif trade_type == "incoming_history":
        url_type = "incoming"
        history = True
        filter_status = ['accepted', 'canceled', 'declined', 'expired', 'inactive', 'countered']
        title = "📋 ИСТОРИЯ ВХОДЯЩИХ ТРЕЙДОВ"

    elif trade_type == "outgoing_active":
        url_type = "outgoing"
        history = False
        filter_status = ['active']
        title = "📤 ИСХОДЯЩИЕ ТРЕЙДЫ (АКТИВНЫЕ)"

    elif trade_type == "outgoing_history":
        url_type = "outgoing"
        history = True
        filter_status = ['accepted', 'canceled', 'declined', 'expired', 'inactive', 'countered']
        title = "📋 ИСТОРИЯ ИСХОДЯЩИХ ТРЕЙДОВ"
    else:
        return

    status_msg = await callback.message.edit_text(
        f"🔄 <b>Загружаю трейды (страница {page})...</b>",
        parse_mode="HTML"
    )

    try:
        # Получаем HTML и парсим
        html_text = await client.get_trade_offers_page(
            get_received=(url_type == "incoming"),
            active_only=False,
            history=history
        )
        all_offers = await client.parse_trade_offers_list(html_text)

        # Фильтруем по статусу
        offers = [o for o in all_offers if o['status'] in filter_status]

        if not offers:
            await status_msg.edit_text(
                f"📭 <b>Нет трейдов в этом разделе</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"trades_{trade_type}_{mafile_id}_1")],
                        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"trades_menu_{mafile_id}")]
                    ]
                )
            )
            return

        # Пагинация
        total_offers = len(offers)
        total_pages = (total_offers + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE

        if page < 1:
            page = 1
        if page > total_pages:
            page = total_pages

        start_idx = (page - 1) * ITEMS_PER_PAGE
        end_idx = start_idx + ITEMS_PER_PAGE
        page_offers = offers[start_idx:end_idx]

        text = f"{title} ({total_offers})\n"
        text += f"📄 Страница {page} из {total_pages}\n\n"

        keyboard_buttons = []

        for idx, offer in enumerate(page_offers, start=start_idx + 1):
            status_icon = {
                'active': '⏳',
                'accepted': '✅',
                'canceled': '❌',
                'declined': '❌',
                'expired': '⌛',
                'inactive': '⏸️',
                'countered': '🔄'
            }.get(offer['status'], '❓')

            tradeofferid = str(offer['tradeofferid'])
            safe_tradeofferid = escape(tradeofferid)
            trade_url = f"https://steamcommunity.com/tradeoffer/{tradeofferid}/"
            text += (
                f"{idx}. {status_icon} <b>Трейд "
                f"<a href='{escape(trade_url, quote=True)}'>"
                f"№{safe_tradeofferid}</a></b>\n"
            )

            # Информация об отправителе/получателе
            partner_id = offer['partner_id']
            partner_name = str(offer['partner_name'] or "Unknown")

            if url_type == "incoming":
                direction = "От"
            else:
                direction = "Кому"

            # Определяем display_name
            if partner_name and partner_name != 'Unknown':
                display_name = partner_name
            else:
                display_name = "Unknown"

            if partner_id:
                # Если partner_id начинается с http - это кастомная ссылка
                partner_id = str(partner_id)
                if partner_id.startswith('https://steamcommunity.com/'):
                    profile_url = partner_id
                    # Для отображения берем последнюю часть URL
                    id_display = partner_id.split('/')[-1]
                else:
                    # Числовой Steam ID
                    profile_url = f"https://steamcommunity.com/profiles/{partner_id}"
                    id_display = partner_id

                text += (
                    f"   👤 <b>{direction}:</b> {escape(display_name)} "
                    f"(<a href='{escape(profile_url, quote=True)}'>"
                    f"{escape(str(id_display))}</a>)\n"
                )
            else:
                text += f"   👤 <b>{direction}:</b> {escape(display_name)}\n"

            # Сообщение
            if offer['message']:
                msg = escape(str(offer['message']))
                text += f"   💬 <b>Комментарий:</b> <i>\"{msg}\"</i>\n"

            # Предметы
            if offer['your_items_count'] > 0:
                if url_type == "incoming":
                    text += f"   📤 Вы отдадите: {escape(str(offer['your_items_count']))} предм.\n"
                else:
                    text += f"   📥 Вы получите: {escape(str(offer['your_items_count']))} предм.\n"
            if offer['their_items_count'] > 0:
                if url_type == "incoming":
                    text += f"   📥 Вы получите: {escape(str(offer['their_items_count']))} предм.\n"
                else:
                    text += f"   📤 Вы отдадите: {escape(str(offer['their_items_count']))} предм.\n"

            # Статус + дата
            status_line = f"   📊 Статус: {escape(str(offer['status_text']))}"
            if offer.get('accepted_date'):
                status_line += f" | 📅 {escape(str(offer['accepted_date']))}"
            text += status_line + "\n"

            # Срок защиты
            if offer.get('protected_until'):
                text += f"   🔒 Предметы защищены до {escape(str(offer['protected_until']))}\n"

            # Срок истечения
            if offer.get('expires_text'):
                text += f"   ⏰ {escape(str(offer['expires_text']))}\n"

            text += "\n"

            # 🔥 Кнопка для просмотра деталей (display_name уже гарантированно определена)
            if offer['status'] == 'active':
                button_text = f"👁️ Открыть трейд №{tradeofferid} от {display_name}"
                if len(button_text) > 64:
                    button_text = f"👁️ Трейд №{tradeofferid} от {display_name[:30]}..."

                detail_callback = _trade_detail_callback(
                    mafile_id, tradeofferid, url_type
                )
                if detail_callback is not None:
                    keyboard_buttons.append([
                        InlineKeyboardButton(
                            text=button_text,
                            callback_data=detail_callback,
                        )
                    ])

        # Легенда
        text += "━━━━━━━━━━━━━━━━━━━━\n"
        text += "⏳ активен | ✅ принят | ❌ отменен/отклонен | ⌛ истек"

        # Навигация
        nav_buttons = []
        if page > 1:
            nav_buttons.append(InlineKeyboardButton(
                text="◀️ Назад",
                callback_data=f"trades_{trade_type}_{mafile_id}_{page - 1}"
            ))
        nav_buttons.append(InlineKeyboardButton(
            text=f"📄 {page}/{total_pages}",
            callback_data="page_info"
        ))
        if page < total_pages:
            nav_buttons.append(InlineKeyboardButton(
                text="Вперед ▶️",
                callback_data=f"trades_{trade_type}_{mafile_id}_{page + 1}"
            ))
        if nav_buttons:
            keyboard_buttons.append(nav_buttons)

        keyboard_buttons.append([
            InlineKeyboardButton(text="🔄 Обновить", callback_data=f"trades_{trade_type}_{mafile_id}_1")
        ])
        keyboard_buttons.append([
            InlineKeyboardButton(text="◀️ Назад", callback_data=f"trades_menu_{mafile_id}")
        ])

        await status_msg.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons),
            disable_web_page_preview=True
        )

    except Exception as e:
        logger.error(f"Error loading trades: {e}", exc_info=True)
        await status_msg.edit_text(
            f"❌ Ошибка при загрузке трейдов\n\n"
            f"<code>{escape(str(e)[:200])}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(text="◀️ Назад", callback_data=f"trades_menu_{mafile_id}")
                ]]
            )
        )

@dp.callback_query(F.data.startswith("trade_accept_"))
@validated_callback_data(
    r"trade_accept_(?P<db_id>[1-9][0-9]{0,9})_"
    r"(?P<uint64_trade>[1-9][0-9]{0,19})"
)
async def callback_trade_accept(callback: CallbackQuery):
    """Принятие трейда с обработкой needauth"""
    parts = callback.data.split("_")
    mafile_id = int(parts[2])
    tradeofferid = parts[3]
    retry_callback = _trade_accept_callback(mafile_id, tradeofferid)
    assert retry_callback is not None
    telegram_id = callback.from_user.id

    # Проверяем наличие прокси
    has_proxy, _ = await check_proxy_available(telegram_id, for_steam_requests=True)
    if not has_proxy:
        await callback_feedback(callback, "❌ Нет доступного прокси!", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            await callback_feedback(callback, "Принимаю трейд...")

            # Проверяем тип сообщения перед редактированием
            if callback.message.photo:
                await callback.message.delete()
                status_msg = await bot.send_message(
                    chat_id=telegram_id,
                    text=f"🔄 <b>Принимаю трейд №{tradeofferid}...</b>",
                    parse_mode="HTML"
                )
            else:
                status_msg = await callback.message.edit_text(
                    f"🔄 <b>Принимаю трейд №{tradeofferid}...</b>",
                    parse_mode="HTML"
                )

            result = await client.accept_trade_offer(tradeofferid)

            # 🔥 Показываем статус восстановления сессии
            await show_recovery_status(callback, client)

            # 🔥 ОБРАБАТЫВАЕМ NEEDAUTH В ПЕРВУЮ ОЧЕРЕДЬ
            if result.get('needauth'):
                await status_msg.edit_text(
                    f"❌ <b>Требуется вход в аккаунт!</b>\n\n"
                    f"Сессия устарела. Выполните вход заново.\n\n"
                    f"<i>Выберите действие:</i>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(text="🔑 Войти по паролю", callback_data=f"action_login_{mafile_id}"),
                                InlineKeyboardButton(text="🔄 Войти по токену", callback_data=f"try_refresh_{mafile_id}")
                            ],
                            [InlineKeyboardButton(
                                text="🔄 Попробовать снова",
                                callback_data=retry_callback
                            )],
                            [InlineKeyboardButton(
                                text="◀️ К списку трейдов",
                                callback_data=f"back_to_trades_list_{mafile_id}_incoming"
                            )]
                        ]
                    )
                )
                return

            # 🔥 ОБРАБАТЫВАЕМ УСПЕХ
            if result.get('success'):
                # Трейд принят
                if result.get('needs_mobile_confirmation'):
                    # Требуется подтверждение в мобильном приложении (Steam Guard)
                    text = (
                        f"✅ <b>Трейд №{tradeofferid} принят!</b>\n\n"
                        f"⚠️ <b>Требуется подтверждение в Steam Guard</b>\n\n"
                        f"<i>Нажмите кнопку ниже, чтобы перейти к подтверждениям:</i>"
                    )

                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="✅ ПЕРЕЙТИ К ПОДТВЕРЖДЕНИЯМ",
                                callback_data=f"confirmations_menu_{mafile_id}"
                            )],
                            [InlineKeyboardButton(
                                text="📋 К списку трейдов",
                                callback_data=f"back_to_trades_list_{mafile_id}_incoming"
                            )],
                            [InlineKeyboardButton(
                                text="🏠 В меню трейдов",
                                callback_data=f"trades_menu_{mafile_id}"
                            )]
                        ]
                    )

                elif result.get('needs_email_confirmation'):
                    # Только email подтверждение (без Steam Guard)
                    email_domain = result.get('email_domain', 'неизвестный домен')
                    text = (
                        f"✅ <b>Трейд №{tradeofferid} принят!</b>\n\n"
                        f"📧 <b>Требуется подтверждение по email</b>\n"
                        f"📨 Домен почты: <code>{escape(str(email_domain))}</code>\n\n"
                        f"<i>Проверьте почту и подтвердите трейд.</i>"
                    )

                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="🔄 Обновить список трейдов",
                                callback_data=f"trades_incoming_active_{mafile_id}_1"
                            )],
                            [InlineKeyboardButton(
                                text="📋 К списку трейдов",
                                callback_data=f"back_to_trades_list_{mafile_id}_incoming"
                            )],
                            [InlineKeyboardButton(
                                text="🏠 В меню трейдов",
                                callback_data=f"trades_menu_{mafile_id}"
                            )]
                        ]
                    )

                elif result.get('tradeid'):
                    # Трейд сразу выполнен (без подтверждения)
                    text = (
                        f"✅ <b>Трейд №{tradeofferid} успешно принят!</b>\n\n"
                        f"🆔 ID сделки: <code>{escape(str(result['tradeid']))}</code>\n\n"
                        f"📦 Предметы будут доставлены в ближайшее время."
                    )

                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="🔄 Обновить список трейдов",
                                callback_data=f"trades_incoming_active_{mafile_id}_1"
                            )],
                            [InlineKeyboardButton(
                                text="📋 К списку трейдов",
                                callback_data=f"back_to_trades_list_{mafile_id}_incoming"
                            )],
                            [InlineKeyboardButton(
                                text="🏠 В меню трейдов",
                                callback_data=f"trades_menu_{mafile_id}"
                            )]
                        ]
                    )
                else:
                    # Успешно, но без дополнительной информации
                    text = (
                        f"✅ <b>Трейд №{tradeofferid} принят!</b>\n\n"
                        f"Проверьте статус трейда в списке."
                    )

                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="🔄 Обновить список",
                                callback_data=f"trades_incoming_active_{mafile_id}_1"
                            )],
                            [InlineKeyboardButton(
                                text="📋 К списку трейдов",
                                callback_data=f"back_to_trades_list_{mafile_id}_incoming"
                            )],
                            [InlineKeyboardButton(
                                text="🏠 В меню трейдов",
                                callback_data=f"trades_menu_{mafile_id}"
                            )]
                        ]
                    )

                await status_msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
                return

            # 🔥 ОБРАБАТЫВАЕМ ОШИБКУ
            error = result.get('error', 'Неизвестная ошибка')

            # Обрабатываем известные ошибки
            error_messages = {
                'logged in from a new device':
                    '❌ Вход выполнен с нового устройства.\n'
                    'Трейды будут доступны через 7 дней.',

                'trade offer has been cancelled':
                    '❌ Трейд был отменен или уже недействителен.',

                'must have a mobile authenticator':
                    '❌ Требуется мобильный аутентификатор для трейдов.',

                'items are no longer available':
                    '❌ Предметы больше недоступны для обмена.',

                'trade offer is no longer valid':
                    '❌ Трейд больше не действителен.',

                'cannot trade with':
                    '❌ Невозможно совершить обмен с этим пользователем.',

                'inventory is not available':
                    '❌ Инвентарь недоступен.',

                'escrow':
                    '❌ Трейд будет удержан (Escrow).',

                'trade hold':
                    '❌ На трейд наложен холд.',
            }

            friendly_error = error
            for key, msg in error_messages.items():
                if key.lower() in error.lower():
                    friendly_error = msg
                    break

            # 🔥 Проверяем на needauth в тексте ошибки
            if 'login' in error.lower() or 'unauthorized' in error.lower() or 'auth' in error.lower():
                error_text = (
                    f"❌ <b>Требуется вход в аккаунт!</b>\n\n"
                    f"<code>{escape(str(error)[:200])}</code>\n\n"
                    f"<i>Выполните вход заново и попробуйте снова.</i>"
                )

                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(text="🔑 Войти по паролю", callback_data=f"action_login_{mafile_id}"),
                            InlineKeyboardButton(text="🔄 Войти по токену", callback_data=f"try_refresh_{mafile_id}")
                        ],
                        [InlineKeyboardButton(
                            text="🔄 Попробовать снова",
                            callback_data=retry_callback
                        )],
                        [InlineKeyboardButton(
                            text="◀️ К списку трейдов",
                            callback_data=f"back_to_trades_list_{mafile_id}_incoming"
                        )]
                    ]
                )
            else:
                error_text = (
                    f"❌ <b>Не удалось принять трейд №{tradeofferid}</b>\n\n"
                    f"{escape(str(friendly_error))}\n\n"
                    f"<i>Детали ошибки:</i>\n"
                    f"<code>{escape(str(error)[:200])}</code>"
                )

                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="🔄 Попробовать снова",
                            callback_data=retry_callback
                        )],
                        [InlineKeyboardButton(
                            text="🔗 Открыть в Steam",
                            url=f"https://steamcommunity.com/tradeoffer/{tradeofferid}/"
                        )],
                        [
                            InlineKeyboardButton(text="🔑 Войти по паролю", callback_data=f"action_login_{mafile_id}"),
                            InlineKeyboardButton(text="🔄 Войти по токену", callback_data=f"try_refresh_{mafile_id}")
                        ],
                        [InlineKeyboardButton(
                            text="◀️ К списку трейдов",
                            callback_data=f"back_to_trades_list_{mafile_id}_incoming"
                        )]
                    ]
                )

            await status_msg.edit_text(error_text, parse_mode="HTML", reply_markup=keyboard)

        except Exception as e:
            logger.error(f"Error in trade accept callback: {e}", exc_info=True)

            error_text = (
                f"❌ <b>Ошибка при принятии трейда</b>\n\n"
                f"<code>{escape(str(e)[:300])}</code>"
            )

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="🔄 Попробовать снова",
                        callback_data=retry_callback
                    )],
                    [
                        InlineKeyboardButton(text="🔑 Войти по паролю", callback_data=f"action_login_{mafile_id}"),
                        InlineKeyboardButton(text="🔄 Войти по токену", callback_data=f"try_refresh_{mafile_id}")
                    ],
                    [InlineKeyboardButton(
                        text="◀️ К списку трейдов",
                        callback_data=f"back_to_trades_list_{mafile_id}_incoming"
                    )]
                ]
            )

            # Пробуем удалить фото если есть
            try:
                if callback.message.photo:
                    await callback.message.delete()
            except:
                pass

            await bot.send_message(
                chat_id=telegram_id,
                text=error_text,
                parse_mode="HTML",
                reply_markup=keyboard
            )

        finally:
            await client.close()
            await callback_feedback(callback)

@dp.callback_query(F.data.startswith("trade_decline_"))
@validated_callback_data(
    r"trade_decline_(?P<db_id>[1-9][0-9]{0,9})_"
    r"(?P<uint64_trade>[1-9][0-9]{0,19})"
)
async def callback_trade_decline(callback: CallbackQuery):
    """Отклонение входящего трейда"""
    parts = callback.data.split("_")
    mafile_id = int(parts[2])
    tradeofferid = parts[3]
    telegram_id = callback.from_user.id
    decline_callback = _trade_decline_callback(mafile_id, tradeofferid)
    if decline_callback is None:
        await callback_feedback(callback, "Некорректный запрос", show_alert=True)
        return

    # Проверяем наличие прокси
    has_proxy, _ = await check_proxy_available(telegram_id, for_steam_requests=True)
    if not has_proxy:
        await callback_feedback(callback, "❌ Нет доступного прокси!", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            await callback_feedback(callback, "Отклоняю трейд...")

            status_msg = await callback.message.edit_text(
                f"🔄 <b>Отклоняю трейд №{tradeofferid}...</b>",
                parse_mode="HTML"
            )

            result = await client.decline_trade_offer(tradeofferid)

            # 🔥 Показываем статус восстановления сессии
            await show_recovery_status(callback, client)

            # 🔥 Обрабатываем needauth
            if result.get('needauth'):
                await status_msg.edit_text(
                    f"❌ <b>Требуется вход в аккаунт!</b>\n\n"
                    f"Сессия устарела. Выполните вход заново.\n\n"
                    f"<i>Выберите действие:</i>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(text="🔑 Войти по паролю", callback_data=f"action_login_{mafile_id}"),
                                InlineKeyboardButton(text="🔄 Войти по токену", callback_data=f"try_refresh_{mafile_id}")
                            ],
                            [InlineKeyboardButton(
                                text="🔄 Попробовать снова",
                                callback_data=decline_callback,
                            )],
                            [InlineKeyboardButton(
                                text="◀️ К списку трейдов",
                                callback_data=f"back_to_trades_list_{mafile_id}_incoming"
                            )]
                        ]
                    )
                )
                return

            if result.get('success'):
                await status_msg.edit_text(
                    f"✅ <b>Трейд №{tradeofferid} успешно отклонен!</b>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="🔄 Обновить список трейдов",
                                callback_data=f"trades_incoming_active_{mafile_id}_1"
                            )],
                            [InlineKeyboardButton(
                                text="◀️ Назад к меню трейдов",
                                callback_data=f"trades_menu_{mafile_id}"
                            )]
                        ]
                    )
                )
            else:
                error = result.get('error', 'Неизвестная ошибка')
                await status_msg.edit_text(
                    f"❌ <b>Не удалось отклонить трейд №{tradeofferid}</b>\n\n"
                    f"<code>{escape(str(error)[:300])}</code>\n\n"
                    f"<i>Попробуйте отклонить трейд напрямую в Steam</i>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="🔗 Открыть в Steam",
                                url=f"https://steamcommunity.com/tradeoffer/{tradeofferid}/"
                            )],
                            [InlineKeyboardButton(
                                text="🔄 Попробовать снова",
                                callback_data=decline_callback,
                            )],
                            [
                                InlineKeyboardButton(text="🔑 Войти по паролю", callback_data=f"action_login_{mafile_id}"),
                                InlineKeyboardButton(text="🔄 Войти по токену", callback_data=f"try_refresh_{mafile_id}")
                            ],
                            [InlineKeyboardButton(
                                text="◀️ К списку трейдов",
                                callback_data=f"back_to_trades_list_{mafile_id}_incoming"
                            )]
                        ]
                    )
                )

        except Exception as e:
            logger.error(f"Error in trade decline callback: {e}", exc_info=True)
            await callback.message.edit_text(
                f"❌ <b>Ошибка при отклонении трейда</b>\n\n"
                f"<code>{escape(str(e)[:300])}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(text="🔑 Войти по паролю", callback_data=f"action_login_{mafile_id}"),
                            InlineKeyboardButton(text="🔄 Войти по токену", callback_data=f"try_refresh_{mafile_id}")
                        ],
                        [InlineKeyboardButton(
                            text="◀️ К списку трейдов",
                            callback_data=f"back_to_trades_list_{mafile_id}_incoming"
                        )]
                    ]
                )
            )

        finally:
            await client.close()
            await callback_feedback(callback)


@dp.callback_query(F.data.startswith("trade_cancel_"))
@validated_callback_data(
    r"trade_cancel_(?P<db_id>[1-9][0-9]{0,9})_"
    r"(?P<uint64_trade>[1-9][0-9]{0,19})"
)
async def callback_trade_cancel_outgoing(callback: CallbackQuery):
    """Отмена исходящего трейда"""
    parts = callback.data.split("_")
    mafile_id = int(parts[2])
    tradeofferid = parts[3]

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            await callback_feedback(callback, "Отменяю трейд...")

            result = await client.cancel_trade_offer(tradeofferid)

            if result.get('success'):
                await callback.message.edit_text(
                    f"✅ <b>Трейд №{tradeofferid} отменен!</b>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[
                            InlineKeyboardButton(text="◀️ К трейдам", callback_data=f"trades_outgoing_active_{mafile_id}_1")
                        ]]
                    )
                )
            else:
                await callback.message.edit_text(
                    f"❌ <b>Не удалось отменить трейд</b>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[
                            InlineKeyboardButton(text="◀️ Назад", callback_data=f"trades_outgoing_active_{mafile_id}_1")
                        ]]
                    )
                )

        except Exception as e:
            await callback_feedback(callback, f"Ошибка: {str(e)[:50]}", show_alert=True)
        finally:
            await client.close()




@dp.callback_query(F.data.startswith("trades_incoming_active_"))
@validated_callback_data(
    r"trades_incoming_active_(?P<db_id>[1-9][0-9]{0,9})_"
    r"(?P<int32_page>[1-9][0-9]{0,9})"
)
async def callback_trades_incoming_active(callback: CallbackQuery):
    """Активные входящие трейды"""
    parts = callback.data.split("_")
    mafile_id = int(parts[3])
    page = int(parts[4]) if len(parts) > 4 else 1

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile or not mafile.access_token:
            await callback_feedback(callback, "Аккаунт не найден или нет сессии", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)
        try:
            await fetch_and_display_trades(callback, mafile_id, client, "incoming_active", page)
        finally:
            await client.close()
            await callback_feedback(callback)


@dp.callback_query(F.data.startswith("trades_incoming_history_"))
@validated_callback_data(
    r"trades_incoming_history_(?P<db_id>[1-9][0-9]{0,9})_"
    r"(?P<int32_page>[1-9][0-9]{0,9})"
)
async def callback_trades_incoming_history(callback: CallbackQuery):
    """История входящих трейдов"""
    parts = callback.data.split("_")
    mafile_id = int(parts[3])
    page = int(parts[4]) if len(parts) > 4 else 1

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile or not mafile.access_token:
            await callback_feedback(callback, "Аккаунт не найден или нет сессии", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)
        try:
            await fetch_and_display_trades(callback, mafile_id, client, "incoming_history", page)
        finally:
            await client.close()
            await callback_feedback(callback)


@dp.callback_query(F.data.startswith("trades_outgoing_active_"))
@validated_callback_data(
    r"trades_outgoing_active_(?P<db_id>[1-9][0-9]{0,9})_"
    r"(?P<int32_page>[1-9][0-9]{0,9})"
)
async def callback_trades_outgoing_active(callback: CallbackQuery):
    """Активные исходящие трейды"""
    parts = callback.data.split("_")
    mafile_id = int(parts[3])
    page = int(parts[4]) if len(parts) > 4 else 1

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile or not mafile.access_token:
            await callback_feedback(callback, "Аккаунт не найден или нет сессии", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)
        try:
            await fetch_and_display_trades(callback, mafile_id, client, "outgoing_active", page)
        finally:
            await client.close()
            await callback_feedback(callback)


@dp.callback_query(F.data.startswith("trades_outgoing_history_"))
@validated_callback_data(
    r"trades_outgoing_history_(?P<db_id>[1-9][0-9]{0,9})_"
    r"(?P<int32_page>[1-9][0-9]{0,9})"
)
async def callback_trades_outgoing_history(callback: CallbackQuery):
    """История исходящих трейдов"""
    parts = callback.data.split("_")
    mafile_id = int(parts[3])
    page = int(parts[4]) if len(parts) > 4 else 1

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile or not mafile.access_token:
            await callback_feedback(callback, "Аккаунт не найден или нет сессии", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)
        try:
            await fetch_and_display_trades(callback, mafile_id, client, "outgoing_history", page)
        finally:
            await client.close()
            await callback_feedback(callback)


@dp.callback_query(F.data.startswith("trade_detail_full_"))
@validated_callback_data(
    r"trade_detail_full_(?P<db_id>[1-9][0-9]{0,9})_"
    r"(?P<uint64_trade>[1-9][0-9]{0,19})_(?:incoming|outgoing)"
)
async def callback_trade_detail_full(callback: CallbackQuery):
    """Детальный просмотр трейда с генерацией изображения"""
    parts = callback.data.split("_")
    mafile_id = int(parts[3])
    tradeofferid = parts[4]
    url_type = parts[5] if len(parts) > 5 else "incoming"
    decline_callback = _trade_decline_callback(mafile_id, tradeofferid)
    cancel_callback = _trade_cancel_callback(mafile_id, tradeofferid)
    if decline_callback is None or cancel_callback is None:
        await callback_feedback(callback, "Некорректный запрос", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, callback.from_user.id)
        if not mafile:
            await callback_feedback(callback, "Аккаунт не найден", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            await callback.message.edit_text(
                "🔄 <b>Загружаю детали трейда...</b>",
                parse_mode="HTML"
            )

            # Парсим страницу
            info = await client.parse_trade_offer_page_full(tradeofferid)

            # Генерируем изображение
            image_bytes = await client.generate_trade_image(info)

            await callback.message.delete()


            trade_url = f"https://steamcommunity.com/tradeoffer/{tradeofferid}/"

            # Формируем подпись
            caption = f"🔄 <b>ТРЕЙД №{escape(str(tradeofferid))}</b>\n\n"

            # Информация о партнёре со ссылкой на профиль
            partner_name = info.get('partner_name') or info.get('partner_steamid', 'Unknown')
            partner_steamid = info.get('partner_steamid')

            caption += f"👤 <b>От:</b> {escape(str(partner_name))}"
            if partner_steamid:
                profile_url = f"https://steamcommunity.com/profiles/{partner_steamid}"
                caption += (
                    f" (<a href='{escape(profile_url, quote=True)}'>"
                    f"{escape(str(partner_steamid))}</a>)"
                )
            caption += "\n"

            # Steam Level
            if info.get('partner_level'):
                caption += f"📊 <b>Steam Level:</b> {escape(str(info['partner_level']))}\n"

            # Друзья с
            if info.get('friends_since'):
                caption += f"🤝 <b>Друзья с:</b> {escape(str(info['friends_since']))}\n"
            else:
                caption += f"🤝 <b>Друзья с:</b> Не в друзьях\n"

            # Дата регистрации
            if info.get('partner_member_since'):
                caption += f"📅 <b>В Steam с:</b> {escape(str(info['partner_member_since']))}\n"

            # Бейдж
            if info.get('badge'):
                caption += f"🏅 <b>Бейдж:</b> {escape(str(info['badge']))}\n"

            caption += "\n"

            # Комментарий к трейду
            if info.get('message'):
                msg = escape(str(info['message']))
                caption += f"💬 <b>Комментарий:</b>\n<i>\"{msg}\"</i>\n\n"
            else:
                caption += "💬 <b>Комментарий:</b> <i>отсутствует</i>\n\n"

            # Предметы
            caption += "📦 <b>ПРЕДМЕТЫ:</b>\n"
            caption += (
                "📥 Вы получите: "
                f"<b>{escape(str(info.get('their_items_count', 0)))}</b> предм.\n"
            )
            caption += (
                "📤 Вы отдадите: "
                f"<b>{escape(str(info.get('your_items_count', 0)))}</b> предм.\n"
            )

            # Кнопки
            keyboard_buttons = []
            accept_callback = _trade_accept_callback(mafile_id, tradeofferid)

            # 🔥 Первая строка: Принять и Отклонить (для входящих)
            if url_type == "incoming" and accept_callback is not None:
                keyboard_buttons.append([
                    InlineKeyboardButton(
                        text="✅ Принять трейд",
                        callback_data=accept_callback,
                    ),
                    InlineKeyboardButton(
                        text="❌ Отклонить",
                        callback_data=decline_callback,
                    )
                ])
            else:
                keyboard_buttons.append([
                    InlineKeyboardButton(
                        text="❌ Отменить трейд",
                        callback_data=cancel_callback,
                    )
                ])

            # 🔥 Вторая строка: Открыть в браузере
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text="🔗 Открыть трейд в Steam",
                    url=trade_url
                )
            ])

            # 🔥 Третья строка: Все трейды
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text="📋 Все трейды",
                    callback_data=f"back_to_trades_list_{mafile_id}_{url_type}"
                )
            ])

            await callback.message.answer_photo(
                BufferedInputFile(image_bytes, filename=f"trade_{tradeofferid}.png"),
                caption=caption,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
            )

        except Exception as e:
            logger.error(f"Error generating trade image: {e}", exc_info=True)

            # 🔥 ИСПРАВЛЕНО: url_type гарантированно строка
            back_type = url_type if url_type else "incoming"
            trade_url = f"https://steamcommunity.com/tradeoffer/{tradeofferid}/"

            keyboard_buttons = []
            accept_callback = _trade_accept_callback(mafile_id, tradeofferid)

            if back_type == "incoming" and accept_callback is not None:
                keyboard_buttons.append([
                    InlineKeyboardButton(
                        text="✅ Принять трейд",
                        callback_data=accept_callback,
                    ),
                    InlineKeyboardButton(
                        text="❌ Отклонить",
                        callback_data=decline_callback,
                    )
                ])
            else:
                keyboard_buttons.append([
                    InlineKeyboardButton(
                        text="❌ Отменить трейд",
                        callback_data=cancel_callback,
                    )
                ])

            keyboard_buttons.append([
                InlineKeyboardButton(
                    text="🔗 Открыть в браузере",
                    url=trade_url
                )
            ])

            keyboard_buttons.append([
                InlineKeyboardButton(
                    text="◀️ Назад к списку",
                    callback_data=f"back_to_trades_list_{mafile_id}_{back_type}"
                )
            ])

            await callback.message.edit_text(
                f"❌ Ошибка при создании изображения\n\n"
                f"<code>{escape(str(e)[:200])}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
            )
        finally:
            await client.close()
            await callback_feedback(callback)


@dp.callback_query(F.data.startswith("back_to_trades_list_"))
@validated_callback_data(
    r"back_to_trades_list_(?P<db_id>[1-9][0-9]{0,9})_(?:incoming|outgoing)"
)
async def callback_back_to_trades_list(callback: CallbackQuery):
    """Удаляет текущее сообщение и открывает список трейдов"""
    parts = callback.data.split("_")
    mafile_id = int(parts[4])
    url_type = parts[5]

    # Удаляем текущее сообщение (с фото или ошибкой)
    await callback.message.delete()

    # Отправляем НОВОЕ сообщение через bot (не через callback.message)
    msg = await bot.send_message(
        chat_id=callback.from_user.id,
        text="🔄 <b>Загружаю трейды...</b>",
        parse_mode="HTML"
    )

    # Создаём новый callback с правильным message
    # Вместо подмены message, вызываем функцию напрямую с новым сообщением
    if url_type == "incoming":
        await fetch_and_redirect_trades(callback, msg, mafile_id, "incoming_active", 1)
    else:
        await fetch_and_redirect_trades(callback, msg, mafile_id, "outgoing_active", 1)

    await callback_feedback(callback)


async def fetch_and_redirect_trades(original_callback: CallbackQuery, new_msg: Message,
                                    mafile_id: int, trade_type: str, page: int):
    """Вспомогательная функция для загрузки трейдов в новое сообщение"""
    async with AsyncSessionLocal() as session:
        mafile = await get_owned_mafile(session, mafile_id, original_callback.from_user.id)
        if not mafile or not mafile.access_token:
            await new_msg.edit_text("❌ Аккаунт не найден или нет сессии")
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)
        try:
            # Создаём фейковый callback с новым сообщением
            class FakeCallback:
                def __init__(self, message, from_user, data):
                    self.message = message
                    self.from_user = from_user
                    self.data = data

                async def answer(self, text=None, show_alert=False):
                    pass

            fake_callback = FakeCallback(
                message=new_msg,
                from_user=original_callback.from_user,
                data=f"trades_{trade_type}_{mafile_id}_{page}"
            )

            await fetch_and_display_trades(fake_callback, mafile_id, client, trade_type, page)
        finally:
            await client.close()

async def _reject_secret_callback_outside_private_chat(
    callback: CallbackQuery,
) -> bool:
    if callback.message and callback.message.chat.type == ChatType.PRIVATE:
        return False
    await callback_feedback(
        callback,
        "❌ Ввод секретов доступен только в личном чате с ботом.",
        show_alert=True,
    )
    return True


async def _start_bot_password_prompt(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    action: str,
    text: str,
) -> None:
    if await _reject_secret_callback_outside_private_chat(callback):
        return

    await callback_feedback(callback)
    await clear_user_workflows(callback.from_user.id, state)
    try:
        if action not in {"set", "change", "remove"}:
            raise ValueError("Unsupported bot-password action")
        await state.set_state(BotPasswordFlow.waiting_password)
        await state.set_data({"action": action})
        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="❌ Отмена",
                            callback_data="cancel_password_to_settings",
                        )
                    ]
                ]
            ),
        )
    except (Exception, asyncio.CancelledError):
        await clear_user_workflows(callback.from_user.id, state)
        raise


@dp.callback_query(F.data == "set_password")
async def callback_set_password(callback: CallbackQuery, state: FSMContext):
    """Запрос на установку пароля"""
    await _start_bot_password_prompt(
        callback,
        state,
        action="set",
        text=(
            "🔑 <b>УСТАНОВКА ПАРОЛЯ</b>\n\n"
            "Отправьте новый пароль для блокировки бота.\n\n"
            "<i>Минимум 8 символов. Пароль хранится как защищённый хеш.</i>\n\n"
            "Для отмены нажмите кнопку ниже:"
        ),
    )


@dp.callback_query(F.data == "change_password")
async def callback_change_password(callback: CallbackQuery, state: FSMContext):
    """Запрос на изменение пароля"""
    await _start_bot_password_prompt(
        callback,
        state,
        action="change",
        text=(
            "🔑 <b>ИЗМЕНЕНИЕ ПАРОЛЯ</b>\n\n"
            "Отправьте новый пароль для блокировки бота.\n\n"
            "<i>Минимум 8 символов. Старый пароль будет заменён.</i>\n\n"
            "Для отмены нажмите кнопку ниже:"
        ),
    )


@dp.callback_query(F.data == "remove_password")
async def callback_remove_password(callback: CallbackQuery, state: FSMContext):
    """Запрос на удаление пароля"""
    await _start_bot_password_prompt(
        callback,
        state,
        action="remove",
        text=(
            "🗑️ <b>УДАЛЕНИЕ ПАРОЛЯ</b>\n\n"
            "Введите текущий пароль для подтверждения удаления.\n\n"
            "Для отмены нажмите кнопку ниже:"
        ),
    )


@dp.callback_query(F.data == "cancel_password_to_settings")
async def callback_cancel_password_to_settings(
    callback: CallbackQuery,
    state: FSMContext,
):
    """Clear password-entry state before returning to settings."""
    await clear_user_workflows(callback.from_user.id, state)
    await callback_feedback(callback)
    from guardbot.bot.handlers.accounts import update_settings_message

    await update_settings_message(callback.message, callback.from_user.id)


@dp.callback_query(F.data == "lock_bot")
async def callback_lock_bot(callback: CallbackQuery, state: FSMContext):
    """Мгновенная блокировка бота"""
    telegram_id = callback.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if not user or not user.bot_password:
            await callback_feedback(callback, "❌ Сначала установите пароль!", show_alert=True)
            return

        user.last_activity = None  # Сбрасываем активность, что вызовет блокировку
        await session.commit()

    await callback_feedback(callback, "🔒 Бот заблокирован!")
    await clear_legacy_user_state(telegram_id)
    await state.clear()
    await callback.message.edit_text(
        "🔒 <b>БОТ ЗАБЛОКИРОВАН</b>\n\n"
        "Для разблокировки используйте:\n"
        "<code>/unlock [пароль]</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔓 Разблокировать", callback_data="unlock_bot")]
            ]
        )
    )


@dp.callback_query(F.data == "unlock_bot")
async def callback_unlock_bot(callback: CallbackQuery, state: FSMContext):
    """Запрос пароля для разблокировки"""
    if await _reject_secret_callback_outside_private_chat(callback):
        return

    await callback_feedback(callback)
    await clear_legacy_user_state(callback.from_user.id)
    await state.set_state(UnlockFlow.waiting_password)

    try:
        await callback.message.edit_text(
            "🔓 <b>РАЗБЛОКИРОВКА</b>\n\n"
            "Отправьте пароль для разблокировки бота.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_unlock")]
                ]
            )
        )
    except Exception:
        await state.clear()
        raise


@dp.callback_query(F.data == "set_lock_timeout")
async def callback_set_lock_timeout(callback: CallbackQuery):
    """Настройка таймаута автоблокировки"""
    telegram_id = callback.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        current_timeout = user.lock_timeout if user and user.lock_timeout else 0

    await callback.message.edit_text(
        f"⏱ <b>АВТОБЛОКИРОВКА</b>\n\n"
        f"Текущий таймаут: <b>{current_timeout} мин</b> {'(выключено)' if current_timeout == 0 else ''}\n\n"
        f"Выберите время бездействия, после которого бот автоматически заблокируется:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text=f"{'✅ ' if current_timeout == 5 else ''}5 минут",
                    callback_data="set_timeout_5"
                )],
                [InlineKeyboardButton(
                    text=f"{'✅ ' if current_timeout == 15 else ''}15 минут",
                    callback_data="set_timeout_15"
                )],
                [InlineKeyboardButton(
                    text=f"{'✅ ' if current_timeout == 30 else ''}30 минут",
                    callback_data="set_timeout_30"
                )],
                [InlineKeyboardButton(
                    text=f"{'✅ ' if current_timeout == 60 else ''}1 час",
                    callback_data="set_timeout_60"
                )],
                [InlineKeyboardButton(
                    text="🔓 Выключить",
                    callback_data="set_timeout_0"
                )],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="settings")]
            ]
        )
    )
    await callback_feedback(callback)
