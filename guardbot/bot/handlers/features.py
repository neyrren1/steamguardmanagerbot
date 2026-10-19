"""Extracted from the legacy bot module without behavior changes."""

from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from guardbot.bot.callback_validation import validated_callback_data
from guardbot.bot.runtime import (
    bot,
    callback_feedback,
    check_proxy_available,
    close_owned_resource,
    clear_user_workflows,
    show_recovery_status,
)
from guardbot.config import logger
from guardbot.database import AsyncSessionLocal, Mafile
from guardbot.services.ownership import get_owned_mafile
from guardbot.services.session_manager import SteamSessionManager
from sqlalchemy import text
import re

router = Router(name=__name__)


from guardbot.bot.handlers.callback_builders import (
    _bounded_identifier,
    _confirmation_callback,
    _inventory_page_callback,
    _item_detail_callback,
    _trade_accept_callback,
    _trade_cancel_callback,
    _trade_decline_callback,
    _trade_detail_callback,
)








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

@router.callback_query(F.data.startswith("trade_accept_"))
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

@router.callback_query(F.data.startswith("trade_decline_"))
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


@router.callback_query(F.data.startswith("trade_cancel_"))
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




@router.callback_query(F.data.startswith("trades_incoming_active_"))
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


@router.callback_query(F.data.startswith("trades_incoming_history_"))
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


@router.callback_query(F.data.startswith("trades_outgoing_active_"))
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


@router.callback_query(F.data.startswith("trades_outgoing_history_"))
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


@router.callback_query(F.data.startswith("trade_detail_full_"))
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


@router.callback_query(F.data.startswith("back_to_trades_list_"))
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


from guardbot.bot.handlers.trade_notifications import (
    _start_trade_notification_interval_flow,
    callback_trade_notifications_menu,
    callback_trade_notify_interval,
    callback_trade_notify_toggle,
    callback_trades_menu,
    process_trade_notification_interval,
)  # noqa: F401 - compatibility exports for guardbot.compat and existing imports


from guardbot.bot.handlers.trade_link import callback_tradelink  # noqa: F401 - compatibility export


from guardbot.bot.handlers.confirmations import (
    callback_cancel_all,
    callback_cancel_single,
    callback_confirm_all,
    callback_confirm_single,
    callback_confirmations_menu,
)  # noqa: F401 - compatibility exports for commands and existing imports


from guardbot.bot.handlers.inventory import (
    callback_inventory_app,
    callback_inventory_menu,
    callback_item_detail,
    process_inventory_items,
    process_inventory_items_simple,
)  # noqa: F401 - compatibility exports for guardbot.compat and existing imports


from guardbot.bot.handlers.security_controls import (
    _reject_secret_callback_outside_private_chat,
    _start_bot_password_prompt,
    callback_cancel_password_to_settings,
    callback_change_password,
    callback_lock_bot,
    callback_remove_password,
    callback_set_lock_timeout,
    callback_set_password,
    callback_unlock_bot,
)  # noqa: F401 - compatibility exports for guardbot.compat and existing imports
