"""Steam inventory browsing and item-detail handlers."""

import re
from html import escape
from urllib.parse import quote

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from guardbot.bot.callback_validation import validated_callback_data
from guardbot.bot.handlers.callback_builders import (
    _inventory_page_callback,
    _item_detail_callback,
)
from guardbot.bot.runtime import (
    callback_feedback,
    calculate_steam_price,
    check_proxy_available,
    close_owned_resource,
    clear_user_workflows,
    get_item_price,
    get_inventory_context_id,
    process_inventory_items_universal,
)
from guardbot.config import STEAM_CURRENCIES, logger
from guardbot.database import AsyncSessionLocal
from guardbot.services.ownership import get_owned_mafile
from guardbot.services.session_manager import SteamSessionManager

router = Router(name=__name__)


# ---------- ИНВЕНТАРЬ ----------
@router.callback_query(F.data.startswith("inventory_menu_"))
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



@router.callback_query(F.data.startswith("inventory_app_"))
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

@router.callback_query(F.data.startswith("item_detail_"))
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
