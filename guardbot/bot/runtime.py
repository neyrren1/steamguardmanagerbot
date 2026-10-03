"""Extracted from the legacy bot module without behavior changes."""

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from datetime import datetime
from guardbot import security
from guardbot.config import STEAM_CURRENCIES, logger
from guardbot.database import AsyncSessionLocal, Mafile, User
from guardbot.security import decrypt_value
from guardbot.services.session_manager import SteamSessionManager
from guardbot.steam.client import AsyncSteamMobile, USER_AGENT_MOBILE
from sqlalchemy import func, select, text
from typing import Dict, Optional
import aiohttp
import asyncio
import json
import math
import re

# ==================== БОТ ====================
dp = Dispatcher()


class BotProxy:
    def __init__(self) -> None:
        self._instance: Optional[Bot] = None

    def configure(self, token: str) -> None:
        self._instance = Bot(token=token)

    def get(self) -> Bot:
        if self._instance is None:
            raise RuntimeError("Telegram bot is not initialized")
        return self._instance

    def __getattr__(self, name: str):
        return getattr(self.get(), name)


bot = BotProxy()


def init_bot():
    """Инициализация бота (вызывается после init_fernet)."""
    bot.configure(security.BOT_TOKEN)
    logger.info("✅ Bot initialized")

class LockCheckMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Update, data: dict):
        # Получаем пользователя
        user_id = None

        if event.message:
            user_id = event.message.from_user.id
            msg = event.message
        elif event.callback_query:
            user_id = event.callback_query.from_user.id
            msg = event.callback_query.message
        else:
            return await handler(event, data)

        # Команды которые работают всегда (даже при блокировке)
        allowed_commands = ['unlock']

        # 🔥 Проверяем callback_query на разблокировку
        if event.callback_query:
            if event.callback_query.data == "unlock_bot":
                return await handler(event, data)

        # 🔥 Проверяем, что пользователь в процессе разблокировки (вводит пароль)
        if user_id in user_states and user_states[user_id].get("state") == "waiting_unlock_password":
            return await handler(event, data)

        # Проверяем команды в сообщениях
        if event.message and event.message.text:
            text = event.message.text.lower()
            if any(text.startswith(f'/{cmd}') for cmd in allowed_commands):
                return await handler(event, data)

        # Проверяем блокировку
        async with AsyncSessionLocal() as session:
            stmt = select(User).where(User.telegram_id == user_id)
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()

            if user and user.bot_password:
                # Проверяем таймаут
                if user.lock_timeout and user.last_activity:
                    timeout_seconds = user.lock_timeout * 60
                    elapsed = (datetime.utcnow() - user.last_activity).total_seconds()

                    if elapsed > timeout_seconds:
                        # Блокируем
                        user.last_activity = None
                        await session.commit()
                        await msg.answer(
                            "🔒 <b>БОТ ЗАБЛОКИРОВАН</b>\n\n"
                            "Сработала автоблокировка по бездействию.\n"
                            "<code>/unlock [пароль]</code> — разблокировать",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(
                                inline_keyboard=[
                                    [InlineKeyboardButton(text="🔓 Разблокировать", callback_data="unlock_bot")]
                                ]
                            )
                        )
                        return

                # Проверяем ручную блокировку
                if user.last_activity is None:
                    await msg.answer(
                        "🔒 <b>БОТ ЗАБЛОКИРОВАН</b>\n\n"
                        "<code>/unlock [пароль]</code> — разблокировать",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(text="🔓 Разблокировать", callback_data="unlock_bot")]
                            ]
                        )
                    )
                    return

                # Обновляем время активности
                user.last_activity = datetime.utcnow()
                await session.commit()

        return await handler(event, data)

dp.update.middleware(LockCheckMiddleware())

user_states = {}

def calculate_steam_price(price_float: float, currency_id: int) -> int:
    """
    Рассчитывает цену для отправки в Steam с учётом комиссии и минимальной комиссии

    Args:
        price_float: цена в валюте (например, 100.0)
        currency_id: ID валюты из STEAM_CURRENCIES

    Returns:
        int: цена в копейках/центах для отправки в Steam
    """
    import math

    # Получаем информацию о валюте
    currency_info = STEAM_CURRENCIES.get(currency_id, {})
    min_commission = currency_info.get('min_commission')  # может быть None

    # 1. Вычисляем цену после процентной комиссии (3/23)
    price_after_percent = price_float * 20 / 23

    # 2. Если минимальная комиссия задана — проверяем
    if min_commission is not None:
        # Вычисляем сумму комиссии в копейках/центах
        commission_amount = (price_float * 100) - (price_after_percent * 100)

        if commission_amount < min_commission:
            # Комиссия меньше минимальной — вычитаем минимальную
            price_floored = math.floor((price_float * 100 - min_commission)) / 100
            logger.info(f"💰 Min commission applied: {min_commission/100:.2f} (regular would be {commission_amount/100:.2f})")
        else:
            # Обычная комиссия 3/23 с округлением вниз
            price_floored = math.floor(price_after_percent * 100) / 100
    else:
        # Минимальная комиссия не задана — используем обычную 3/23
        price_floored = math.floor(price_after_percent * 100) / 100

    # 3. Конвертируем в копейки/центы
    price_to_send = int(price_floored * 100)

    logger.info(f"💰 Price calculation: {price_float} -> after commission: {price_floored} -> send: {price_to_send}")

    return price_to_send

@dp.callback_query(F.data.startswith("sell_item_"))
async def callback_sell_item(callback: CallbackQuery):
    """Продажа предмета по минимальной или средней цене"""
    parts = callback.data.split("_")
    # sell_item_min_3_730_51107204438
    # 0    1    2   3   4    5
    sell_type = parts[2]  # min, median или custom
    mafile_id = int(parts[3])
    app_id = int(parts[4])
    assetid = parts[5]

    telegram_id = callback.from_user.id

    # Проверяем наличие прокси
    has_proxy, _ = await check_proxy_available(telegram_id, for_steam_requests=True)
    if not has_proxy:
        await callback.answer("❌ Нет доступного прокси!", show_alert=True)
        return

    if sell_type == "custom":
        # Запрашиваем пользовательскую цену
        user_states[callback.from_user.id] = {
            "state": "waiting_custom_price",
            "mafile_id": mafile_id,
            "app_id": app_id,
            "assetid": assetid,
            "context_id": 2
        }

        await callback.message.edit_text(
            "💰 <b>Введите цену продажи:</b>\n\n"
            "Отправьте число — цену в вашей валюте (например: <code>8.70</code>)\n\n"
            "<i>Комиссия Steam (13%) будет учтена автоматически.</i>\n\n"
            "Для отмены нажмите кнопку ниже:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data=f"inventory_menu_{mafile_id}"
                    )]
                ]
            )
        )
        await callback.answer()
        return

    # Для min и median - получаем цену и продаём
    async with AsyncSessionLocal() as session:
        mafile = await session.get(Mafile, mafile_id)
        if not mafile:
            await callback.answer("Аккаунт не найден", show_alert=True)
            return

        if not mafile.access_token:
            await callback.answer("Нет активной сессии", show_alert=True)
            return

        # Получаем страну и валюту
        country = mafile.country or "UA"
        currency = mafile.currency_id or 18

        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

        try:
            # Получаем инвентарь чтобы найти market_hash_name
            data = await client.get_inventory(app_id, 2 if app_id != 753 else 6)
            if not data:
                await callback.answer("Не удалось загрузить инвентарь", show_alert=True)
                return

            items = process_inventory_items_universal(data, app_id)
            item = next((i for i in items if i['assetid'] == assetid), None)

            if not item or not item.get('market_hash_name'):
                await callback.answer("Предмет не найден", show_alert=True)
                return

            # Получаем цену
            await client._ensure_session()
            price_data = await get_item_price(client.session, app_id, item['market_hash_name'], country, currency)

            if not price_data:
                await callback.answer("Не удалось получить цену", show_alert=True)
                return

            # Определяем цену для продажи
            if sell_type == "min":
                price_str = price_data['lowest_price']
            else:  # median
                price_str = price_data['median_price']

            # Парсим цену (убираем символ валюты и преобразуем в число)
            import re
            # Извлекаем число (может быть с запятой или точкой)
            price_match = re.search(r'[\d.,]+', price_str.replace(' ', ''))
            if not price_match:
                await callback.answer("Не удалось распарсить цену", show_alert=True)
                return

            price_float = float(price_match.group().replace(',', '.'))

            # Конвертируем в копейки/центы и вычитаем комиссию 3/23%
            price_with_commission = calculate_steam_price(price_float, currency)

            if price_with_commission <= 0:
                await callback.answer("Цена слишком низкая после комиссии", show_alert=True)
                return

            type_text = "минимальной" if sell_type == "min" else "средней"

            await callback.answer(f"Продаю по {type_text} цене: {price_float} -> {price_with_commission/100:.2f} (после комиссии)...")

            status_msg = await callback.message.edit_text(
                f"💰 <b>Продаю предмет по {type_text} цене...</b>\n\n"
                f"📛 <b>Предмет:</b> {item['name']}\n"
                f"💵 <b>Цена:</b> {price_str}\n"
                f"📊 <b>Цена после комиссии (13%):</b> {price_with_commission/100:.2f}\n\n"
                f"<i>Отправляю запрос...</i>",
                parse_mode="HTML"
            )

            # Продаём
            result = await client.sell_item_with_recovery(app_id, 2, assetid, 1, price_with_commission)
            # Показываем статус восстановления сессии
            await show_recovery_status(callback, client)

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
                                text="◀️ К инвентарю",
                                callback_data=f"inventory_menu_{mafile_id}"
                            )]
                        ]
                    )
                )
                return

            if result.get('success'):
                if result.get('needs_mobile_confirmation'):
                    text = (
                        f"✅ <b>Предмет выставлен на продажу!</b>\n\n"
                        f"📛 <b>Предмет:</b> {item['name']}\n"
                        f"💵 <b>Цена:</b> {price_str}\n\n"
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
                                text="🎒 В меню инвентаря",
                                callback_data=f"inventory_menu_{mafile_id}"
                            )]
                        ]
                    )

                elif result.get('needs_email_confirmation'):
                    email_domain = result.get('email_domain', 'неизвестный домен')
                    text = (
                        f"✅ <b>Предмет выставлен на продажу!</b>\n\n"
                        f"📛 <b>Предмет:</b> {item['name']}\n"
                        f"💵 <b>Цена:</b> {price_str}\n\n"
                        f"📧 <b>Требуется подтверждение по email</b>\n"
                        f"📨 Домен почты: <code>{email_domain}</code>\n\n"
                        f"<i>Проверьте почту и подтвердите продажу.</i>"
                    )

                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="🎒 В меню инвентаря",
                                callback_data=f"inventory_menu_{mafile_id}"
                            )]
                        ]
                    )
                else:
                    text = (
                        f"✅ <b>Предмет успешно выставлен на продажу!</b>\n\n"
                        f"📛 <b>Предмет:</b> {item['name']}\n"
                        f"💵 <b>Цена:</b> {price_str}\n\n"
                        f"📦 Предмет появится на торговой площадке в ближайшее время."
                    )

                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="🎒 В меню инвентаря",
                                callback_data=f"inventory_menu_{mafile_id}"
                            )]
                        ]
                    )

                await status_msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
            else:
                error = result.get('error', 'Неизвестная ошибка')
                await status_msg.edit_text(
                    f"❌ <b>Не удалось продать предмет</b>\n\n"
                    f"<code>{error[:300]}</code>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="🔄 Попробовать снова",
                                callback_data=f"sell_item_{sell_type}_{mafile_id}_{app_id}_{assetid}"
                            )],
                            [InlineKeyboardButton(
                                text="🎒 В меню инвентаря",
                                callback_data=f"inventory_menu_{mafile_id}"
                            )]
                        ]
                    )
                )

        except Exception as e:
            logger.error(f"Error in sell item callback: {e}", exc_info=True)
            await callback.answer(f"Ошибка: {str(e)[:100]}", show_alert=True)
        finally:
            await client.close()

async def get_item_price(session: aiohttp.ClientSession, app_id: int, market_hash_name: str, country: str = "UA", currency: int = 18) -> Optional[Dict]:
    """
    Получает цену предмета с Steam Market

    Args:
        session: aiohttp сессия (ДОЛЖНА быть с ProxyConnector для SOCKS5)
        app_id: ID игры (730=CS2)
        market_hash_name: полное имя предмета для маркета
        country: код страны (UA, US, etc.)
        currency: ID валюты (18=UAH)

    Returns:
        dict с ключами lowest_price, median_price, volume или None
    """
    import urllib.parse

    # Кодируем market_hash_name для URL
    encoded_name = urllib.parse.quote(market_hash_name)

    url = f"https://steamcommunity.com/market/priceoverview/"
    params = {
        'country': country,
        'currency': currency,
        'appid': app_id,
        'market_hash_name': market_hash_name
    }

    headers = {
        'User-Agent': USER_AGENT_MOBILE,
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
    }

    try:
        # 🔥 Сессия уже должна быть с правильным коннектором (ProxyConnector для SOCKS5)
        async with session.get(url, params=params, headers=headers, ssl=False) as resp:
            if resp.status != 200:
                try:
                    error_text = await resp.text()
                    logger.warning(f"Price overview HTTP {resp.status} for '{market_hash_name}': {error_text[:300]}")
                except:
                    logger.warning(f"Price overview HTTP {resp.status} for '{market_hash_name}' (no body)")
                return None

            data = await resp.json()

            if data.get('success'):
                logger.info(f"💰 Price for '{market_hash_name}': lowest={data.get('lowest_price')}, median={data.get('median_price')}")
                return {
                    'lowest_price': data.get('lowest_price', 'N/A'),
                    'median_price': data.get('median_price', 'N/A'),
                    'volume': data.get('volume', '0')
                }
            else:
                logger.warning(f"❌ Price overview failed for '{market_hash_name}' (app {app_id}, country {country}, currency {currency})")
                logger.warning(f"Full response: {json.dumps(data, ensure_ascii=False)}")
                return None

    except Exception as e:
        logger.error(f"Error getting price for {market_hash_name}: {e}")
        return None

def process_inventory_items_universal(data: dict, app_id: int) -> list:
    """Универсальная обработка предметов инвентаря для разных игр"""
    import re

    assets = data.get('assets', [])
    descriptions = data.get('descriptions', [])
    asset_properties_list = data.get('asset_properties', [])

    # Словарь описаний по ключу classid_instanceid
    desc_dict = {}
    for desc in descriptions:
        classid = desc.get('classid')
        instanceid = desc.get('instanceid')
        if classid is not None and instanceid is not None:
            key = f"{classid}_{instanceid}"
            desc_dict[key] = desc

    # Словарь свойств по assetid
    properties_dict = {}
    if asset_properties_list:
        for prop in asset_properties_list:
            assetid = str(prop.get('assetid', ''))
            if assetid:
                properties_dict[assetid] = prop

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
        assetid = str(asset.get('assetid', ''))
        classid = asset.get('classid')
        instanceid = asset.get('instanceid')
        amount = int(asset.get('amount', 1))

        desc_key = f"{classid}_{instanceid}"
        desc = desc_dict.get(desc_key, {})

        name = desc.get('market_name') or desc.get('name') or 'Unknown Item'
        market_hash_name = desc.get('market_hash_name', '')

        icon_url = ''
        if desc.get('icon_url'):
            icon_url = f"https://community.akamai.steamstatic.com/economy/image/{desc['icon_url']}"

        rarity_name = ''
        rarity_emoji = '⚪'
        rarity_color = '#b0c3d9'

        for tag in desc.get('tags', []):
            if tag.get('category') == 'Rarity':
                rarity_name = tag.get('localized_tag_name', '')
                rarity_emoji = rarity_to_emoji.get(rarity_name, '⚪')
                color = tag.get('color', '')
                if color:
                    rarity_color = f"#{color}" if not color.startswith('#') else color
                break

        item_type = ''
        for tag in desc.get('tags', []):
            if tag.get('category') == 'Type':
                item_type = tag.get('localized_tag_name', '')
                break

        item = {
            'assetid': assetid,
            'classid': classid,
            'instanceid': instanceid,
            'app_id': app_id,
            'name': name,
            'market_hash_name': market_hash_name,
            'icon_url': icon_url,
            'rarity': rarity_name,
            'rarity_emoji': rarity_emoji,
            'rarity_color': rarity_color,
            'item_type': item_type,
            'tradable': desc.get('tradable', 0) == 1,
            'marketable': desc.get('marketable', 0) == 1,
            'amount': amount,
            'exterior': '',
            'float': None,
            'pattern': None,
            'paint_seed': None,
            'is_stattrak': False,
            'stattrak_count': None,
            'collection': '',
            'stickers': [],
            'hero': '',
            'quality': '',
            'name_tag': None,
            'inspect_link': None,  # 🔥 Новое поле для ссылки на осмотр
        }

        # ========== CS2 / TF2 специфичные поля ==========
        if app_id in [730, 440]:
            # Exterior
            for tag in desc.get('tags', []):
                if tag.get('category') == 'Exterior':
                    item['exterior'] = tag.get('localized_tag_name', '')
                    break

            # StatTrak
            for tag in desc.get('tags', []):
                if tag.get('category') == 'Quality' and 'StatTrak' in tag.get('localized_tag_name', ''):
                    item['is_stattrak'] = True
                    break

            # StatTrak счетчик
            for d in desc.get('descriptions', []):
                value = d.get('value', '')
                if 'StatTrak' in value and ('Kills' in value or 'Kill' in value):
                    match = re.search(r'(\d[\d,]*)', value)
                    if match:
                        try:
                            item['stattrak_count'] = int(match.group(1).replace(',', ''))
                        except:
                            pass
                    break

            # Коллекция
            for tag in desc.get('tags', []):
                if tag.get('category') == 'ItemSet':
                    item['collection'] = tag.get('localized_tag_name', '')
                    break

            # Float, Pattern, Paint Seed, Name Tag И Inspect Link из asset_properties
            asset_props = properties_dict.get(assetid)
            if asset_props:
                props_list = asset_props.get('asset_properties', [])

                for prop in props_list:
                    prop_id = prop.get('propertyid')
                    if prop_id == 1:  # Pattern Template
                        item['pattern'] = prop.get('int_value')
                    elif prop_id == 2:  # Wear Rating (Float)
                        try:
                            item['float'] = float(prop.get('float_value', 0))
                        except (ValueError, TypeError):
                            pass
                    elif prop_id == 3:  # Paint Seed
                        item['paint_seed'] = prop.get('int_value')
                    elif prop_id == 5:  # Name Tag
                        name_tag_raw = prop.get('string_value', '')
                        if name_tag_raw:
                            try:
                                item['name_tag'] = bytes.fromhex(name_tag_raw).decode('utf-8', errors='replace')
                            except:
                                item['name_tag'] = name_tag_raw
                    elif prop_id == 6:  # Item Certificate (ссылка на осмотр)
                        certificate = prop.get('string_value', '')
                        if certificate:
                            item['inspect_link'] = f"steam://rungame/730/{app_id}/+csgo_econ_action_preview%20S{certificate}"

            # 🔥 ПАРСИМ НАКЛЕЙКИ ИЗ sticker_info В ОПИСАНИИ
            sticker_info_html = None
            for d in desc.get('descriptions', []):
                if d.get('name') == 'sticker_info':
                    sticker_info_html = d.get('value', '')
                    break

            if sticker_info_html:
                # Ищем все title атрибуты в img тегах
                sticker_titles = re.findall(r'title="([^"]+)"', sticker_info_html)

                for title in sticker_titles:
                    if title.startswith('Sticker:'):
                        sticker_name = title.replace('Sticker:', '').strip()
                        item['stickers'].append({
                            'name': sticker_name,
                            'icon_url': '',
                            'wear': None
                        })

            # 🔥 Парсим износ из asset_accessories
            # Важно: порядок аксессуаров соответствует порядку стикеров в sticker_info
            if asset_props:
                accessories = asset_props.get('asset_accessories', [])
                # Берем только те аксессуары, у которых есть parent_relationship_properties с propertyid=4
                sticker_wears = []
                for accessory in accessories:
                    for rel_prop in accessory.get('parent_relationship_properties', []):
                        if rel_prop.get('propertyid') == 4:
                            try:
                                wear = float(rel_prop.get('float_value', 0))
                                sticker_wears.append(wear)
                            except (ValueError, TypeError):
                                sticker_wears.append(0)
                            break

                # Применяем износ к стикерам в том же порядке
                for i, wear in enumerate(sticker_wears):
                    if i < len(item['stickers']):
                        item['stickers'][i]['wear'] = wear

            # Также ищем иконки для стикеров из описаний аксессуаров
            if asset_props:
                accessories = asset_props.get('asset_accessories', [])
                # Строим словарь classid -> description для аксессуаров
                acc_classids = set()
                for acc in accessories:
                    acc_classid = str(acc.get('classid', ''))
                    if acc_classid:
                        acc_classids.add(acc_classid)

                acc_desc_dict = {}
                for d in descriptions:
                    if str(d.get('classid', '')) in acc_classids:
                        acc_desc_dict[str(d.get('classid', ''))] = d

                for i, accessory in enumerate(accessories):
                    acc_classid = str(accessory.get('classid', ''))
                    acc_desc = acc_desc_dict.get(acc_classid)
                    if acc_desc and i < len(item['stickers']):
                        icon = acc_desc.get('icon_url', '')
                        if icon:
                            item['stickers'][i]['icon_url'] = f"https://community.akamai.steamstatic.com/economy/image/{icon}"

        # ========== Dota 2 специфичные поля ==========
        elif app_id == 570:
            for tag in desc.get('tags', []):
                if tag.get('category') == 'Quality':
                    item['quality'] = tag.get('localized_tag_name', '')
                    break
            for tag in desc.get('tags', []):
                if tag.get('category') == 'Hero':
                    item['hero'] = tag.get('localized_tag_name', '')
                    break

        # ========== Steam ==========
        elif app_id == 753:
            item['item_type'] = item_type or desc.get('type', '')

        items.append(item)

    return items

# Словарь для хранения запущенных задач проверки трейдов
trade_check_tasks: dict[int, asyncio.Task] = {}

async def check_new_trades_for_account(mafile_id: int):
    """Фоновая задача для проверки новых трейдов"""
    logger.info(f"Запущена проверка трейдов для mafile_id={mafile_id}")

    while True:
        try:
            async with AsyncSessionLocal() as session:
                mafile = await session.get(Mafile, mafile_id)

                # Проверяем, что аккаунт существует и уведомления включены
                if not mafile or not mafile.trade_notifications or not mafile.trade_notify_interval:
                    logger.info(f"Проверка трейдов для {mafile_id} остановлена: уведомления выключены")
                    break

                # Проверяем, что есть токен для запросов
                if not mafile.access_token:
                    logger.warning(f"Нет токена для проверки трейдов {mafile.account_name}")
                    await asyncio.sleep(mafile.trade_notify_interval * 60)
                    continue

                # Проверяем прокси
                has_proxy, _ = await check_proxy_available(mafile.telegram_id, for_steam_requests=True)
                if not has_proxy:
                    logger.warning(f"Нет прокси для проверки трейдов {mafile.account_name}")
                    await asyncio.sleep(mafile.trade_notify_interval * 60)
                    continue

                client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

                try:
                    # Получаем активные входящие трейды
                    html_text = await client.get_trade_offers_page(get_received=True, active_only=False, history=False)
                    offers = await client.parse_trade_offers_list(html_text)

                    # Фильтруем только активные входящие
                    active_offers = [o for o in offers if o['status'] == 'active']

                    # Получаем список известных ID
                    known_ids = set(mafile.known_trade_ids or [])
                    current_ids = set(o['tradeofferid'] for o in active_offers)

                    # Находим новые трейды
                    new_ids = current_ids - known_ids

                    if new_ids:
                        logger.info(f"Найдено {len(new_ids)} новых трейдов для {mafile.account_name}")

                        # Отправляем уведомления
                        new_offers = [o for o in active_offers if o['tradeofferid'] in new_ids]

                        for offer in new_offers:
                            tradeofferid = offer['tradeofferid']
                            partner_name = offer['partner_name'] or 'Unknown'
                            partner_id = offer['partner_id']
                            your_items = offer['your_items_count']
                            their_items = offer['their_items_count']
                            message_text = offer['message'] or 'Отсутствует'

                            # Формируем ссылку на трейд
                            trade_url = f"https://steamcommunity.com/tradeoffer/{tradeofferid}/"

                            # Формируем ссылку на профиль и ID для отображения
                            if partner_id:
                                if partner_id.startswith('http'):
                                    profile_url = partner_id
                                    profile_display = partner_id.split('/')[-1]
                                else:
                                    profile_url = f"https://steamcommunity.com/profiles/{partner_id}"
                                    profile_display = partner_id
                            else:
                                profile_url = trade_url
                                profile_display = "неизвестно"

                            # Экранируем сообщение для HTML
                            message_text = message_text.replace('<', '&lt;').replace('>', '&gt;').replace('&', '&amp;')

                            text = (
                                f"🚨 <b>ПРИШЕЛ НОВЫЙ ТРЕЙД!</b>\n\n"
                                f"⏳ <b>Трейд <a href='{trade_url}'>№{tradeofferid}</a></b>\n"
                                f"📱 <b>На аккаунт:</b> <code>{mafile.account_name}</code>\n"
                                f"👤 <b>От партнера:</b> {partner_name} (<a href='{profile_url}'>{profile_display}</a>)\n"
                                f"📦 <b>Предметов от вас:</b> {your_items}\n"
                                f"📦 <b>Предметов от партнера:</b> {their_items}\n"
                                f"💬 <b>Комментарий:</b> <i>\"{message_text}\"</i>"
                            )

                            # 🔥 ИСПРАВЛЕННАЯ КЛАВИАТУРА
                            keyboard = InlineKeyboardMarkup(
                                inline_keyboard=[
                                    [InlineKeyboardButton(
                                        text="👁️ Посмотреть трейд",
                                        callback_data=f"trade_detail_full_{mafile_id}_{tradeofferid}_incoming"
                                    )],
                                    [
                                        InlineKeyboardButton(
                                            text="✅ Принять",
                                            callback_data=f"trade_accept_{mafile_id}_{tradeofferid}_{partner_id}"
                                        ),
                                        InlineKeyboardButton(
                                            text="❌ Отклонить",
                                            callback_data=f"trade_decline_{mafile_id}_{tradeofferid}"
                                        )
                                    ],
                                    [InlineKeyboardButton(
                                        text="📋 Все трейды",
                                        callback_data=f"trades_incoming_active_{mafile_id}_1"
                                    )]
                                ]
                            )

                            # Отправляем уведомление
                            try:
                                await bot.send_message(
                                    chat_id=mafile.telegram_id,
                                    text=text,
                                    parse_mode="HTML",
                                    reply_markup=keyboard,
                                    disable_web_page_preview=True
                                )
                                logger.info(f"Отправлено уведомление о трейде {tradeofferid} для {mafile.account_name}")
                            except Exception as e:
                                logger.error(f"Ошибка отправки уведомления: {e}")

                    # Обновляем список известных ID
                    mafile.known_trade_ids = list(current_ids)
                    mafile.last_trade_check = datetime.utcnow()
                    await session.commit()

                    logger.info(f"Проверка трейдов для {mafile.account_name}: активно {len(active_offers)}, новых {len(new_ids)}")

                except Exception as e:
                    logger.error(f"Ошибка при проверке трейдов для {mafile.account_name}: {e}", exc_info=True)
                finally:
                    await client.close()

            # Ждем указанный интервал
            interval = mafile.trade_notify_interval if mafile else 5
            logger.info(f"Следующая проверка трейдов для {mafile.account_name} через {interval} мин")
            await asyncio.sleep(interval * 60)

        except asyncio.CancelledError:
            logger.info(f"Проверка трейдов для {mafile_id} отменена")
            break
        except Exception as e:
            logger.error(f"Критическая ошибка в проверке трейдов для {mafile_id}: {e}", exc_info=True)
            await asyncio.sleep(60)  # При критической ошибке ждем минуту и пробуем снова

def start_trade_check_task(mafile_id: int):
    """Запускает задачу проверки трейдов"""
    if mafile_id in trade_check_tasks:
        # Останавливаем старую задачу
        trade_check_tasks[mafile_id].cancel()

    task = asyncio.create_task(check_new_trades_for_account(mafile_id))
    trade_check_tasks[mafile_id] = task
    logger.info(f"Задача проверки трейдов для {mafile_id} создана")


def stop_trade_check_task(mafile_id: int):
    """Останавливает задачу проверки трейдов"""
    task = trade_check_tasks.pop(mafile_id, None)
    if task:
        task.cancel()
        logger.info(f"Задача проверки трейдов для {mafile_id} остановлена")


async def init_trade_check_tasks():
    """Инициализация задач проверки трейдов при запуске бота"""
    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.trade_notifications == True)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        for mafile in mafiles:
            start_trade_check_task(mafile.id)

        logger.info(f"Запущено {len(mafiles)} задач проверки трейдов")


async def check_proxy_available(telegram_id: int, for_steam_requests: bool = True) -> tuple[bool, Optional[str]]:
    """
    Проверяет, есть ли у пользователя хоть какой-то прокси.
    Возвращает (True, proxy) если прокси есть, иначе (False, None)
    """
    logger.info(f"check_proxy_available called with telegram_id={telegram_id}, type={type(telegram_id)}")

    if not for_steam_requests:
        return True, None

    async with AsyncSessionLocal() as session:
        # Проверяем общий прокси
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        logger.info(f"User found: {user is not None}, general_proxy: {user.general_proxy if user else None}")

        if user and user.general_proxy:
            proxy_decrypted = decrypt_value(user.general_proxy)
            logger.info(f"General proxy type: {'SOCKS' if proxy_decrypted.startswith(('socks4://', 'socks5://')) else 'HTTP'}")
            return True, user.general_proxy

        # Проверяем, есть ли аккаунты с уникальными прокси
        stmt = select(Mafile).where(
            Mafile.telegram_id == telegram_id,
            Mafile.unique_proxy == True,
            Mafile.proxy.isnot(None)
        )
        result = await session.execute(stmt)
        mafile_with_proxy = result.scalar_one_or_none()

        logger.info(f"Unique proxy found: {mafile_with_proxy is not None}")

        if mafile_with_proxy:
            return True, None

        return False, None

def require_proxy(func):
    """Декоратор для проверки наличия прокси перед выполнением команды"""
    async def wrapper(message: Message, *args, **kwargs):
        telegram_id = message.from_user.id

        has_proxy, _ = await check_proxy_available(telegram_id, for_steam_requests=True)

        if not has_proxy:
            await message.answer(
                "❌ <b>Нет доступного прокси!</b>\n\n"
                "Для выполнения этого действия необходим прокси.\n\n"
                "<b>Настройте прокси:</b>\n"
                "<code>/set_proxy http://user:pass@host:port</code> - общий прокси\n"
                "<code>/set_account_proxy [acc] [url]</code> - уникальный прокси\n\n"
                "<i>Без прокси вы можете только получать Steam Guard коды.</i>",
                parse_mode="HTML"
            )
            return

        return await func(message, *args, **kwargs)
    return wrapper

async def show_recovery_status(callback: CallbackQuery, client: AsyncSteamMobile):
    """Показывает всплывашки о статусе восстановления сессии"""
    if hasattr(client, '_recovery_attempted') and client._recovery_attempted:
        # Сначала показываем что пробовали
        await callback.answer("🔄 Пробую восстановить сессию...", show_alert=False)
        await asyncio.sleep(0.3)

        if hasattr(client, '_session_recovered') and client._session_recovered:
            await callback.answer("✅ Сессия успешно восстановлена!", show_alert=False)
            client._session_recovered = False
        elif hasattr(client, '_session_recovery_failed') and client._session_recovery_failed:
            await callback.answer("❌ Не удалось восстановить сессию", show_alert=False)
            client._session_recovery_failed = False

        client._recovery_attempted = False

@dp.callback_query(F.data == "back_to_start")
async def process_back_to_start(callback: CallbackQuery, state: FSMContext):
    """Возврат в главное меню"""
    # Очищаем состояние FSM
    current_state = await state.get_state()
    if current_state:
        await state.clear()

    # Очищаем пользовательские состояния
    user_id = callback.from_user.id
    if user_id in user_states:
        user_states.pop(user_id)
        logger.info(f"Cleared state for user {user_id}")

    user = callback.from_user

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(Mafile.telegram_id == user.id)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        accounts_count = len(mafiles)
        accounts_with_2fa = len([mf for mf in mafiles if mf.shared_secret])
        accounts_logged = len([mf for mf in mafiles if (
            mf.access_token and mf.refresh_token and mf.password and mf.password.strip()
        )])

    welcome_text = (
        "👋 <b>Привет, " + (user.full_name or user.username or 'пользователь') + "!</b>\n\n"
        "🎮 <b>Steam Guard Manager Bot</b> - ваш надежный помощник для управления Steam аккаунтами и Steam Guard кодами.\n\n"
        "📊 <b>Ваша статистика:</b>\n"
        "├─ Аккаунтов: <b>" + str(accounts_count) + "</b>\n"
        "├─ С 2FA: <b>" + str(accounts_with_2fa) + "</b>\n"
        "└─ Авторизовано: <b>" + str(accounts_logged) + "</b>\n\n"
        "⚡ <b>Быстрые команды:</b>\n"
        "<code>/gc [аккаунт]</code> - получить Steam Guard код\n"
        "<code>/add</code> - добавить аккаунт\n"
        "<code>/conf [аккаунт]</code> - посмотреть подтверждения на аккаунте\n"
        "<code>/trades [аккаунт]</code> - открыть меню с трейдами\n"
        "<code>/inv [аккаунт]</code> - открыть меню с инвентарем\n"
        "<code>/login [аккаунт]</code> - войти в аккаунт\n\n"
        "Выберите действие ниже 👇"
    )

    keyboard_buttons = []

    # 1. Информация и Инструкция
    keyboard_buttons.append([
        InlineKeyboardButton(text="ℹ️ Информация", callback_data="info"),
        InlineKeyboardButton(text="📚 Инструкция", callback_data="instruction")
    ])

    # 2. Аккаунт(ы)
    if accounts_count == 0:
        keyboard_buttons.append([
            InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="add_account")
        ])
    elif accounts_count == 1:
        mafile = mafiles[0]
        keyboard_buttons.append([
            InlineKeyboardButton(text="👤 Мой аккаунт", callback_data=f"account_detail_{mafile.id}")
        ])
        keyboard_buttons.append([
            InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="add_account")
        ])
    else:
        keyboard_buttons.append([
            InlineKeyboardButton(text="👤 Мои аккаунты", callback_data="my_accounts")
        ])

    # Группы
    if accounts_count > 1:
        keyboard_buttons.append([
            InlineKeyboardButton(text="📂 Группы", callback_data="groups_menu")
        ])

    # 3. Настройки
    keyboard_buttons.append([
        InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")
    ])

    # 4. Импорт
    keyboard_buttons.append([
        InlineKeyboardButton(text="📦 Импорт log:pass", callback_data="bulk_import_menu"),
        InlineKeyboardButton(text="📁 Импорт mafile", callback_data="mafile_instruction")
    ])

    # 5. Получить код
    keyboard_buttons.append([
        InlineKeyboardButton(text="🔐 Получить код", switch_inline_query_current_chat="/gc ")
    ])

    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    await callback.message.edit_text(welcome_text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()
