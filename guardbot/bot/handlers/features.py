"""Extracted from the legacy bot module without behavior changes."""

from aiogram import F
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from guardbot.bot.runtime import bot, calculate_steam_price, check_proxy_available, dp, get_item_price, process_inventory_items_universal, show_recovery_status, stop_trade_check_task, user_states
from guardbot.config import STEAM_CURRENCIES, logger
from guardbot.database import AsyncSessionLocal, Mafile, User
from guardbot.services.session_manager import SteamSessionManager
from sqlalchemy import select, text
import asyncio
import re

# ==================== НОВЫЕ ОБРАБОТЧИКИ ====================
@dp.callback_query(F.data.startswith("trades_menu_"))
async def callback_trades_menu(callback: CallbackQuery):
    """Меню трейдов с новой клавиатурой"""
    mafile_id = int(callback.data.split("_")[2])
    telegram_id = callback.from_user.id

    has_proxy, _ = await check_proxy_available(telegram_id, for_steam_requests=True)
    if not has_proxy:
        await callback.answer("❌ Нет доступного прокси!", show_alert=True)
        await callback.message.answer(
            "❌ <b>Нет доступного прокси!</b>\n\n"
            "Для просмотра трейдов необходим прокси.",
            parse_mode="HTML"
        )
        return

    # Получаем настройки уведомлений
    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
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
    await callback.answer()

@dp.callback_query(F.data.startswith("trade_notifications_menu_"))
async def callback_trade_notifications_menu(callback: CallbackQuery):
    """Меню настройки уведомлений о трейдах"""
    mafile_id = int(callback.data.split("_")[3])

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        status_text = "🟢 Включены" if mafile.trade_notifications else "🔴 Выключены"
        interval_text = f"Интервал: каждые {mafile.trade_notify_interval} мин" if mafile.trade_notify_interval else "Интервал не задан"

        text = (
            f"🔔 <b>УВЕДОМЛЕНИЯ О ТРЕЙДАХ</b>\n\n"
            f"📱 Аккаунт: <code>{mafile.account_name}</code>\n"
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
        await callback.answer()


# Обновите callback_trade_notify_toggle:
@dp.callback_query(F.data.startswith("trade_notify_toggle_"))
async def callback_trade_notify_toggle(callback: CallbackQuery):
    """Включение/выключение уведомлений"""
    parts = callback.data.split("_")
    mafile_id = int(parts[3])
    action = parts[4]

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        if action == "on":
            # Запрашиваем интервал
            user_states[callback.from_user.id] = {
                "state": "waiting_trade_notify_interval",
                "mafile_id": mafile.id,
                "account_name": mafile.account_name
            }

            await callback.message.edit_text(
                f"⏱ <b>Введите интервал проверки в минутах:</b>\n\n"
                f"Аккаунт: <code>{mafile.account_name}</code>\n\n"
                f"Рекомендуемое значение: 1-5 минут\n"
                f"Минимум: 1 минута\n\n"
                f"Отправьте число или нажмите отмену:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="❌ Отмена",
                            callback_data=f"trade_notifications_menu_{mafile_id}"
                        )]
                    ]
                )
            )
            await callback.answer()

        else:  # off
            mafile.trade_notifications = False
            await session.commit()

            # Останавливаем задачу проверки
            stop_trade_check_task(mafile_id)

            await callback.answer("✅ Уведомления выключены")
            await callback_trade_notifications_menu(callback)


@dp.callback_query(F.data.startswith("trade_notify_interval_"))
async def callback_trade_notify_interval(callback: CallbackQuery):
    """Изменение интервала уведомлений"""
    mafile_id = int(callback.data.split("_")[3])

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        user_states[callback.from_user.id] = {
            "state": "waiting_trade_notify_interval",
            "mafile_id": mafile.id,
            "account_name": mafile.account_name,
            "is_update": True
        }

        await callback.message.edit_text(
            f"⏱ <b>Введите новый интервал проверки в минутах:</b>\n\n"
            f"Аккаунт: <code>{mafile.account_name}</code>\n"
            f"Текущий интервал: {mafile.trade_notify_interval} мин\n\n"
            f"Отправьте число:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=f"trade_notifications_menu_{mafile_id}"
                    )]
                ]
            )
        )
        await callback.answer()

@dp.callback_query(F.data.startswith("tradelink_"))
async def callback_tradelink(callback: CallbackQuery):
    """Получение трейд-ссылки"""
    mafile_id = int(callback.data.split("_")[1])

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        if not mafile.access_token:
            await callback.answer("Нет активной сессии", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)
        try:
            tradelink = await client.get_tradelink()

            if tradelink:
                await callback.message.edit_text(
                    f"🔗 <b>Трейд-ссылка:</b>\n\n"
                    f"<code>{tradelink}</code>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[
                            InlineKeyboardButton(text="◀️ Назад", callback_data=f"trades_menu_{mafile_id}")
                        ]]
                    )
                )
            else:
                await callback.answer("Не удалось получить трейд-ссылку", show_alert=True)
        except Exception as e:
            await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)
        finally:
            await client.close()


# ---------- ИНВЕНТАРЬ ----------
@dp.callback_query(F.data.startswith("inventory_menu_"))
async def callback_inventory_menu(callback: CallbackQuery):
    """Меню инвентаря"""
    mafile_id = int(callback.data.split("_")[2])
    telegram_id = callback.from_user.id

    # 🔥 Проверяем наличие прокси
    has_proxy, _ = await check_proxy_available(telegram_id, for_steam_requests=True)
    if not has_proxy:
        await callback.answer("❌ Нет доступного прокси!", show_alert=True)
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
    await callback.answer()



@dp.callback_query(F.data.startswith("inventory_app_"))
async def callback_inventory_app(callback: CallbackQuery):
    """Инвентарь конкретной игры с кнопками предметов"""
    parts = callback.data.split("_")
    mafile_id = int(parts[2])
    app_id = int(parts[3])
    context_id = int(parts[4])

    # Параметры пагинации
    page = int(parts[5]) if len(parts) > 5 else 1
    start_assetid = parts[6] if len(parts) > 6 else None

    game_names = {730: "CS2", 570: "Dota 2", 440: "TF2", 753: "Steam"}
    game_name = game_names.get(app_id, f"App {app_id}")

    ITEMS_PER_PAGE = 10  # Уменьшаем до 10 для кнопок

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        if not mafile.steamid:
            await callback.answer("SteamID не привязан", show_alert=True)
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

                # Сохраняем данные предмета в callback_data
                # Используем assetid для идентификации
                keyboard_buttons.append([InlineKeyboardButton(
                    text=button_text,
                    callback_data=f"item_detail_{mafile_id}_{app_id}_{item['assetid']}_{item['classid']}_{item['instanceid']}"
                )])

            # Навигация
            nav_buttons = []

            if page > 1:
                nav_buttons.append(InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=f"inventory_app_{mafile_id}_{app_id}_{context_id}_{page-1}_prev"
                ))

            nav_buttons.append(InlineKeyboardButton(
                text=f"📄 {page}",
                callback_data="page_info"
            ))

            if more_items and last_assetid:
                nav_buttons.append(InlineKeyboardButton(
                    text="Вперед ▶️",
                    callback_data=f"inventory_app_{mafile_id}_{app_id}_{context_id}_{page+1}_{last_assetid}"
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
            await callback.answer()

@dp.callback_query(F.data.startswith("item_detail_"))
async def callback_item_detail(callback: CallbackQuery):
    """Детальный просмотр предмета инвентаря"""
    parts = callback.data.split("_")
    mafile_id = int(parts[2])
    app_id = int(parts[3])
    assetid = parts[4]

    game_names = {730: "CS2", 570: "Dota 2", 440: "TF2", 753: "Steam"}
    game_name = game_names.get(app_id, f"App {app_id}")

    await callback.answer("Загружаю детали предмета...")

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            data = await client.get_inventory(app_id, 2 if app_id != 753 else 6)

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
                price_data = await get_item_price(client.session, app_id, item['market_hash_name'], country, currency)

            # Формируем текст
            text = f"🎒 <b>ДЕТАЛИ ПРЕДМЕТА</b>\n\n"
            text += f"📛 <b>Название:</b> {item['name']}\n"

            # Name Tag
            if item.get('name_tag'):
                text += f"🏷️ <b>Name Tag:</b> <code>{item['name_tag']}</code>\n"

            text += f"📦 <b>Тип:</b> {item['item_type']}\n"
            text += f"{item['rarity_emoji']} <b>Редкость:</b> {item['rarity']}\n"

            if item['exterior']:
                text += f"🎨 <b>Износ:</b> {item['exterior']}\n"

            if item['collection']:
                text += f"📚 <b>Коллекция:</b> {item['collection']}\n"

            if item['hero']:
                text += f"👤 <b>Герой:</b> {item['hero']}\n"

            if item['quality']:
                text += f"⭐ <b>Качество:</b> {item['quality']}\n"

            # Float
            if item['float'] is not None:
                text += f"\n🔬 <b>Float:</b> <code>{item['float']:.15f}</code>\n"

            # Pattern
            if item['pattern'] is not None:
                text += f"🎯 <b>Pattern:</b> <code>{item['pattern']}</code>\n"

            # Paint Seed
            if item['paint_seed'] is not None:
                text += f"🌱 <b>Paint Seed:</b> <code>{item['paint_seed']}</code>\n"

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

                text += f"\n💰 <b>Первая цена Steam:</b> {price_data['lowest_price']} <i>(после комиссии: {min_after_str})</i>\n"
                text += f"📊 <b>Средняя цена Steam:</b> {price_data['median_price']} <i>(после комиссии: {med_after_str})</i>\n"
                text += f"📈 <b>Продаж за 24ч:</b> {price_data['volume']} шт.\n"

            # StatTrak
            if item['is_stattrak']:
                text += f"\n📊 <b>StatTrak™:</b> "
                if item['stattrak_count'] is not None:
                    text += f"<code>{item['stattrak_count']}</code> убийств\n"
                else:
                    text += "да\n"

            # Обмен/Продажа
            text += f"\n🔄 <b>Обмен:</b> {'✅ Да' if item['tradable'] else '❌ Нет'}\n"
            text += f"💰 <b>Продажа:</b> {'✅ Да' if item['marketable'] else '❌ Нет'}\n"

            if item['amount'] > 1:
                text += f"📦 <b>Количество:</b> {item['amount']}\n"

            # ССЫЛКА НА ОСМОТР
            if item.get('inspect_link'):
                text += f"\n🔍 <b>Осмотр в игре:</b>\n<code>{item['inspect_link']}</code>\n"

            # НАКЛЕЙКИ С ИЗНОСОМ
            if item['stickers']:
                text += f"\n<b>🏷️ НАКЛЕЙКИ ({len(item['stickers'])}):</b>\n"
                for i, sticker in enumerate(item['stickers'], 1):
                    if sticker['wear'] is not None and sticker['wear'] > 0:
                        wear_percent = round(sticker['wear'] * 100)
                        text += f"  {i}. {sticker['name']} ({wear_percent}%)\n"
                    else:
                        text += f"  {i}. {sticker['name']}\n"

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
                market_url = f"https://steamcommunity.com/market/listings/{app_id}/{item['market_hash_name']}"
                keyboard_buttons.append([
                    InlineKeyboardButton(text="💹 Открыть на Steam Market", url=market_url)
                ])

            keyboard_buttons.append([
                InlineKeyboardButton(
                    text="◀️ Назад к инвентарю",
                    callback_data=f"inventory_app_{mafile_id}_{app_id}_2_1"
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
                f"❌ Ошибка при загрузке предмета\n\n{str(e)[:200]}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[[
                        InlineKeyboardButton(text="◀️ Назад", callback_data=f"inventory_menu_{mafile_id}")
                    ]]
                )
            )
        finally:
            await client.close()

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
async def callback_confirmations_menu(callback: CallbackQuery, send_new: bool = False):
    """Просмотр подтверждений"""
    mafile_id = int(callback.data.split("_")[2])
    telegram_id = callback.from_user.id

    logger.info(f"Opening confirmations menu for mafile_id={mafile_id}, user={telegram_id}")

    # Проверяем наличие прокси
    has_proxy, _ = await check_proxy_available(telegram_id, for_steam_requests=True)
    if not has_proxy:
        await callback.answer("❌ Нет доступного прокси!", show_alert=True)
        await callback.message.answer(
            "❌ <b>Нет доступного прокси!</b>\n\n"
            "Для работы с подтверждениями необходим прокси.",
            parse_mode="HTML"
        )
        return

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.id == mafile_id)
        result = await session.execute(stmt)
        mafile = result.scalar_one_or_none()

        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        if not mafile.identity_secret:
            await callback.answer("Нет identity_secret для подтверждений", show_alert=True)
            return

        if not mafile.access_token:
            await callback.answer("Нет активной сессии. Сначала войдите в аккаунт", show_alert=True)
            await callback.message.edit_text(
                f"❌ <b>Нет активной сессии!</b>\n\n"
                f"Сначала войдите в аккаунт:\n"
                f"<code>/login {mafile.account_name}</code>",
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
                    conf_key = conf.get('nonce')
                    conf_type = conf.get('type')
                    headline = conf.get('headline', '')
                    summary = conf.get('summary', [])
                    type_name = conf.get('type_name', '')

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

                    if conf_id and conf_key:
                        keyboard_buttons.append([
                            InlineKeyboardButton(
                                text=f"✅ {btn_label}",
                                callback_data=f"confirm_single_{mafile_id}_{conf_id}_{conf_key}"
                            ),
                            InlineKeyboardButton(
                                text=f"❌ {btn_label}",
                                callback_data=f"cancel_single_{mafile_id}_{conf_id}_{conf_key}"
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
                    f"<code>{error_text[:100]}</code>",
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
                f"<code>{str(e)[:100]}</code>",
                parse_mode="HTML",
                reply_markup=keyboard
            )

        finally:
            await client.close()

# 🔥 Обработчики для подтверждения/отмены по одному

@dp.callback_query(F.data.startswith("confirm_single_"))
async def callback_confirm_single(callback: CallbackQuery):
    parts = callback.data.split("_")
    mafile_id = int(parts[2])
    conf_id = parts[3]
    conf_key = parts[4]

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            await callback.answer("Подтверждаю...")

            # 🔥 ДЕЛАЕМ КАК В confirm_all - получаем свежий список
            data = await client.get_trade_confirmations()

            if not data or not data.get('success'):
                await callback.answer("❌ Не удалось загрузить подтверждения", show_alert=True)
                return

            # Находим нужное подтверждение
            target_conf = None
            for conf in data.get('conf', []):
                if conf.get('id') == conf_id:
                    target_conf = conf
                    break

            if not target_conf:
                await callback.answer("❌ Подтверждение не найдено", show_alert=True)
                return

            # Используем СВЕЖИЙ conf_key из списка
            fresh_conf_id = target_conf.get('id')
            fresh_conf_key = target_conf.get('nonce')

            # Отправляем подтверждение
            result = await client.send_confirmation(fresh_conf_id, fresh_conf_key, allow=True)

            if result:
                await callback.answer("✅ Подтверждено!", show_alert=False)
                await SteamSessionManager.save_steam_session_to_db(session, mafile.id, client)
            else:
                await callback.answer("❌ Ошибка подтверждения", show_alert=True)
                return

            await callback_confirmations_menu(callback, send_new=True)

        except Exception as e:
            logger.error(f"Error confirming single: {e}", exc_info=True)
            await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)
        finally:
            await client.close()

@dp.callback_query(F.data.startswith("cancel_single_"))
async def callback_cancel_single(callback: CallbackQuery):
    """Отмена одного действия - РАБОТАЕТ КАК confirm_all"""
    parts = callback.data.split("_")
    mafile_id = int(parts[2])
    conf_id = parts[3]
    conf_key = parts[4]

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            await callback.answer("Отменяю...")

            # 🔥 КАК В confirm_all - БЕЗ get_trade_confirmations()
            result = await client.send_confirmation(conf_id, conf_key, allow=False)

            if result:
                await callback.answer("✅ Отменено!", show_alert=False)
                await SteamSessionManager.save_steam_session_to_db(session, mafile.id, client)
            else:
                await callback.answer("❌ Ошибка отмены", show_alert=True)
                return

            await callback_confirmations_menu(callback, send_new=True)

        except Exception as e:
            logger.error(f"Error canceling single: {e}", exc_info=True)
            await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)
        finally:
            await client.close()

@dp.callback_query(F.data.startswith("confirm_all_"))
async def callback_confirm_all(callback: CallbackQuery):
    """Подтвердить все ожидающие действия"""
    mafile_id = int(callback.data.split("_")[2])

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.id == mafile_id)
        result = await session.execute(stmt)
        mafile = result.scalar_one_or_none()

        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            await callback.answer("Начинаю подтверждение...")

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
                f"❌ Ошибка: {str(e)[:100]}",
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
async def callback_cancel_all(callback: CallbackQuery):
    """Отменить все ожидающие действия"""
    mafile_id = int(callback.data.split("_")[2])

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.id == mafile_id)
        result = await session.execute(stmt)
        mafile = result.scalar_one_or_none()

        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            await callback.answer("Отменяю все действия...")

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
                f"❌ Ошибка: {str(e)[:100]}",
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

            trade_url = f"https://steamcommunity.com/tradeoffer/{offer['tradeofferid']}/"
            text += f"{idx}. {status_icon} <b>Трейд <a href='{trade_url}'>№{offer['tradeofferid']}</a></b>\n"

            # Информация об отправителе/получателе
            partner_id = offer['partner_id']
            partner_name = offer['partner_name']

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
                if partner_id.startswith('http'):
                    profile_url = partner_id
                    # Для отображения берем последнюю часть URL
                    id_display = partner_id.split('/')[-1]
                else:
                    # Числовой Steam ID
                    profile_url = f"https://steamcommunity.com/profiles/{partner_id}"
                    id_display = partner_id

                text += f"   👤 <b>{direction}:</b> {display_name} (<a href='{profile_url}'>{id_display}</a>)\n"
            else:
                text += f"   👤 <b>{direction}:</b> {display_name}\n"

            # Сообщение
            if offer['message']:
                msg = offer['message'].replace('<', '&lt;').replace('>', '&gt;').replace('&', '&amp;')
                text += f"   💬 <b>Комментарий:</b> <i>\"{msg}\"</i>\n"

            # Предметы
            if offer['your_items_count'] > 0:
                if url_type == "incoming":
                    text += f"   📤 Вы отдадите: {offer['your_items_count']} предм.\n"
                else:
                    text += f"   📥 Вы получите: {offer['your_items_count']} предм.\n"
            if offer['their_items_count'] > 0:
                if url_type == "incoming":
                    text += f"   📥 Вы получите: {offer['their_items_count']} предм.\n"
                else:
                    text += f"   📤 Вы отдадите: {offer['their_items_count']} предм.\n"

            # Статус + дата
            status_line = f"   📊 Статус: {offer['status_text']}"
            if offer.get('accepted_date'):
                status_line += f" | 📅 {offer['accepted_date']}"
            text += status_line + "\n"

            # Срок защиты
            if offer.get('protected_until'):
                text += f"   🔒 Предметы защищены до {offer['protected_until']}\n"

            # Срок истечения
            if offer.get('expires_text'):
                text += f"   ⏰ {offer['expires_text']}\n"

            text += "\n"

            # 🔥 Кнопка для просмотра деталей (display_name уже гарантированно определена)
            if offer['status'] == 'active':
                button_text = f"👁️ Открыть трейд №{offer['tradeofferid']} от {display_name}"
                if len(button_text) > 64:
                    button_text = f"👁️ Трейд №{offer['tradeofferid']} от {display_name[:30]}..."

                keyboard_buttons.append([
                    InlineKeyboardButton(
                        text=button_text,
                        callback_data=f"trade_detail_full_{mafile_id}_{offer['tradeofferid']}_{url_type}"
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
            f"❌ Ошибка при загрузке трейдов\n\n<code>{str(e)[:200]}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(text="◀️ Назад", callback_data=f"trades_menu_{mafile_id}")
                ]]
            )
        )

@dp.callback_query(F.data.startswith("trade_accept_"))
async def callback_trade_accept(callback: CallbackQuery):
    """Принятие трейда с обработкой needauth"""
    parts = callback.data.split("_")
    mafile_id = int(parts[2])
    tradeofferid = parts[3]
    partner_id = parts[4] if len(parts) > 4 and parts[4] else None
    telegram_id = callback.from_user.id

    # Проверяем наличие прокси
    has_proxy, _ = await check_proxy_available(telegram_id, for_steam_requests=True)
    if not has_proxy:
        await callback.answer("❌ Нет доступного прокси!", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            await callback.answer("Принимаю трейд...")

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

            result = await client.accept_trade_offer(tradeofferid, partner_id)

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
                                callback_data=f"trade_accept_{mafile_id}_{tradeofferid}_{partner_id or ''}"
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
                        f"📨 Домен почты: <code>{email_domain}</code>\n\n"
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
                        f"🆔 ID сделки: <code>{result['tradeid']}</code>\n\n"
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
                    f"<code>{error[:200]}</code>\n\n"
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
                            callback_data=f"trade_accept_{mafile_id}_{tradeofferid}_{partner_id or ''}"
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
                    f"{friendly_error}\n\n"
                    f"<i>Детали ошибки:</i>\n"
                    f"<code>{error[:200]}</code>"
                )

                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="🔄 Попробовать снова",
                            callback_data=f"trade_accept_{mafile_id}_{tradeofferid}_{partner_id or ''}"
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
                f"<code>{str(e)[:300]}</code>"
            )

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="🔄 Попробовать снова",
                        callback_data=f"trade_accept_{mafile_id}_{tradeofferid}_{partner_id or ''}"
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
            await callback.answer()

@dp.callback_query(F.data.startswith("trade_decline_"))
async def callback_trade_decline(callback: CallbackQuery):
    """Отклонение входящего трейда"""
    parts = callback.data.split("_")
    mafile_id = int(parts[2])
    tradeofferid = parts[3]
    telegram_id = callback.from_user.id

    # Проверяем наличие прокси
    has_proxy, _ = await check_proxy_available(telegram_id, for_steam_requests=True)
    if not has_proxy:
        await callback.answer("❌ Нет доступного прокси!", show_alert=True)
        return

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            await callback.answer("Отклоняю трейд...")

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
                                callback_data=f"trade_decline_{mafile_id}_{tradeofferid}"
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
                    f"<code>{error[:300]}</code>\n\n"
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
                                callback_data=f"trade_decline_{mafile_id}_{tradeofferid}"
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
                f"<code>{str(e)[:300]}</code>",
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
            await callback.answer()


@dp.callback_query(F.data.startswith("trade_cancel_"))
async def callback_trade_cancel_outgoing(callback: CallbackQuery):
    """Отмена исходящего трейда"""
    parts = callback.data.split("_")
    mafile_id = int(parts[2])
    tradeofferid = parts[3]

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            await callback.answer("Отменяю трейд...")

            result = await client.cancel_trade_offer(tradeofferid)

            if result.get('success'):
                await callback.message.edit_text(
                    f"✅ <b>Трейд №{tradeofferid} отменен!</b>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[
                            InlineKeyboardButton(text="◀️ К трейдам", callback_data=f"trades_outgoing_{mafile_id}")
                        ]]
                    )
                )
            else:
                await callback.message.edit_text(
                    f"❌ <b>Не удалось отменить трейд</b>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[[
                            InlineKeyboardButton(text="◀️ Назад", callback_data=f"trades_outgoing_{mafile_id}")
                        ]]
                    )
                )

        except Exception as e:
            await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)
        finally:
            await client.close()




@dp.callback_query(F.data.startswith("trades_incoming_active_"))
async def callback_trades_incoming_active(callback: CallbackQuery):
    """Активные входящие трейды"""
    parts = callback.data.split("_")
    mafile_id = int(parts[3])
    page = int(parts[4]) if len(parts) > 4 else 1

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile or not mafile.access_token:
            await callback.answer("Аккаунт не найден или нет сессии", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)
        try:
            await fetch_and_display_trades(callback, mafile_id, client, "incoming_active", page)
        finally:
            await client.close()
            await callback.answer()


@dp.callback_query(F.data.startswith("trades_incoming_history_"))
async def callback_trades_incoming_history(callback: CallbackQuery):
    """История входящих трейдов"""
    parts = callback.data.split("_")
    mafile_id = int(parts[3])
    page = int(parts[4]) if len(parts) > 4 else 1

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile or not mafile.access_token:
            await callback.answer("Аккаунт не найден или нет сессии", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)
        try:
            await fetch_and_display_trades(callback, mafile_id, client, "incoming_history", page)
        finally:
            await client.close()
            await callback.answer()


@dp.callback_query(F.data.startswith("trades_outgoing_active_"))
async def callback_trades_outgoing_active(callback: CallbackQuery):
    """Активные исходящие трейды"""
    parts = callback.data.split("_")
    mafile_id = int(parts[3])
    page = int(parts[4]) if len(parts) > 4 else 1

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile or not mafile.access_token:
            await callback.answer("Аккаунт не найден или нет сессии", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)
        try:
            await fetch_and_display_trades(callback, mafile_id, client, "outgoing_active", page)
        finally:
            await client.close()
            await callback.answer()


@dp.callback_query(F.data.startswith("trades_outgoing_history_"))
async def callback_trades_outgoing_history(callback: CallbackQuery):
    """История исходящих трейдов"""
    parts = callback.data.split("_")
    mafile_id = int(parts[3])
    page = int(parts[4]) if len(parts) > 4 else 1

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile or not mafile.access_token:
            await callback.answer("Аккаунт не найден или нет сессии", show_alert=True)
            return

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)
        try:
            await fetch_and_display_trades(callback, mafile_id, client, "outgoing_history", page)
        finally:
            await client.close()
            await callback.answer()


@dp.callback_query(F.data.startswith("trade_detail_full_"))
async def callback_trade_detail_full(callback: CallbackQuery):
    """Детальный просмотр трейда с генерацией изображения"""
    parts = callback.data.split("_")
    mafile_id = int(parts[3])
    tradeofferid = parts[4]
    url_type = parts[5] if len(parts) > 5 else "incoming"

    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
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
            caption = f"🔄 <b>ТРЕЙД №{tradeofferid}</b>\n\n"

            # Информация о партнёре со ссылкой на профиль
            partner_name = info.get('partner_name') or info.get('partner_steamid', 'Unknown')
            partner_steamid = info.get('partner_steamid')

            caption += f"👤 <b>От:</b> {partner_name}"
            if partner_steamid:
                profile_url = f"https://steamcommunity.com/profiles/{partner_steamid}"
                caption += f" (<a href='{profile_url}'>{partner_steamid}</a>)"
            caption += "\n"

            # Steam Level
            if info.get('partner_level'):
                caption += f"📊 <b>Steam Level:</b> {info['partner_level']}\n"

            # Друзья с
            if info.get('friends_since'):
                caption += f"🤝 <b>Друзья с:</b> {info['friends_since']}\n"
            else:
                caption += f"🤝 <b>Друзья с:</b> Не в друзьях\n"

            # Дата регистрации
            if info.get('partner_member_since'):
                caption += f"📅 <b>В Steam с:</b> {info['partner_member_since']}\n"

            # Бейдж
            if info.get('badge'):
                caption += f"🏅 <b>Бейдж:</b> {info['badge']}\n"

            caption += "\n"

            # Комментарий к трейду
            if info.get('message'):
                msg = info['message'].replace('<', '&lt;').replace('>', '&gt;')
                caption += f"💬 <b>Комментарий:</b>\n<i>\"{msg}\"</i>\n\n"
            else:
                caption += "💬 <b>Комментарий:</b> <i>отсутствует</i>\n\n"

            # Предметы
            caption += "📦 <b>ПРЕДМЕТЫ:</b>\n"
            caption += f"📥 Вы получите: <b>{info.get('their_items_count', 0)}</b> предм.\n"
            caption += f"📤 Вы отдадите: <b>{info.get('your_items_count', 0)}</b> предм.\n"

            # Кнопки
            keyboard_buttons = []

            # 🔥 Первая строка: Принять и Отклонить (для входящих)
            if url_type == "incoming":
                keyboard_buttons.append([
                    InlineKeyboardButton(
                        text="✅ Принять трейд",
                        callback_data=f"trade_accept_{mafile_id}_{tradeofferid}_{info.get('partner_steamid', '')}"
                    ),
                    InlineKeyboardButton(
                        text="❌ Отклонить",
                        callback_data=f"trade_decline_{mafile_id}_{tradeofferid}"
                    )
                ])
            else:
                keyboard_buttons.append([
                    InlineKeyboardButton(
                        text="❌ Отменить трейд",
                        callback_data=f"trade_cancel_{mafile_id}_{tradeofferid}"
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

            if back_type == "incoming":
                keyboard_buttons.append([
                    InlineKeyboardButton(
                        text="✅ Принять трейд",
                        callback_data=f"trade_accept_{mafile_id}_{tradeofferid}_"
                    ),
                    InlineKeyboardButton(
                        text="❌ Отклонить",
                        callback_data=f"trade_decline_{mafile_id}_{tradeofferid}"
                    )
                ])
            else:
                keyboard_buttons.append([
                    InlineKeyboardButton(
                        text="❌ Отменить трейд",
                        callback_data=f"trade_cancel_{mafile_id}_{tradeofferid}"
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
                f"❌ Ошибка при создании изображения\n\n<code>{str(e)[:200]}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
            )
        finally:
            await client.close()
            await callback.answer()


@dp.callback_query(F.data.startswith("back_to_trades_list_"))
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

    await callback.answer()


async def fetch_and_redirect_trades(original_callback: CallbackQuery, new_msg: Message,
                                    mafile_id: int, trade_type: str, page: int):
    """Вспомогательная функция для загрузки трейдов в новое сообщение"""
    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
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

@dp.callback_query(F.data == "set_password")
async def callback_set_password(callback: CallbackQuery):
    """Запрос на установку пароля"""
    user_states[callback.from_user.id] = {
        "state": "waiting_bot_password",
        "action": "set"
    }

    await callback.message.edit_text(
        "🔑 <b>УСТАНОВКА ПАРОЛЯ</b>\n\n"
        "Отправьте новый пароль для блокировки бота.\n\n"
        "<i>Минимум 4 символа. Пароль будет зашифрован.</i>\n\n"
        "Для отмены нажмите кнопку ниже:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="settings")]
            ]
        )
    )
    await callback.answer()


@dp.callback_query(F.data == "change_password")
async def callback_change_password(callback: CallbackQuery):
    """Запрос на изменение пароля"""
    user_states[callback.from_user.id] = {
        "state": "waiting_bot_password",
        "action": "change"
    }

    await callback.message.edit_text(
        "🔑 <b>ИЗМЕНЕНИЕ ПАРОЛЯ</b>\n\n"
        "Отправьте новый пароль для блокировки бота.\n\n"
        "<i>Минимум 4 символа. Старый пароль будет заменён.</i>\n\n"
        "Для отмены нажмите кнопку ниже:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="settings")]
            ]
        )
    )
    await callback.answer()


@dp.callback_query(F.data == "remove_password")
async def callback_remove_password(callback: CallbackQuery):
    """Запрос на удаление пароля"""
    user_states[callback.from_user.id] = {
        "state": "waiting_bot_password",
        "action": "remove"
    }

    await callback.message.edit_text(
        "🗑️ <b>УДАЛЕНИЕ ПАРОЛЯ</b>\n\n"
        "Введите текущий пароль для подтверждения удаления.\n\n"
        "Для отмены нажмите кнопку ниже:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="settings")]
            ]
        )
    )
    await callback.answer()


@dp.callback_query(F.data == "lock_bot")
async def callback_lock_bot(callback: CallbackQuery):
    """Мгновенная блокировка бота"""
    telegram_id = callback.from_user.id

    async with AsyncSessionLocal() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if not user or not user.bot_password:
            await callback.answer("❌ Сначала установите пароль!", show_alert=True)
            return

        user.last_activity = None  # Сбрасываем активность, что вызовет блокировку
        await session.commit()

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
    await callback.answer("🔒 Бот заблокирован!")


@dp.callback_query(F.data == "unlock_bot")
async def callback_unlock_bot(callback: CallbackQuery):
    """Запрос пароля для разблокировки"""
    user_states[callback.from_user.id] = {
        "state": "waiting_unlock_password"
    }

    await callback.message.edit_text(
        "🔓 <b>РАЗБЛОКИРОВКА</b>\n\n"
        "Отправьте пароль для разблокировки бота.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="lock_bot")]
            ]
        )
    )
    await callback.answer()


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
    await callback.answer()
