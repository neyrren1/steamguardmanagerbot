"""Steam mobile-confirmation handlers."""

import asyncio
from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from guardbot.bot.callback_validation import validated_callback_data
from guardbot.bot.handlers.callback_builders import _confirmation_callback
from guardbot.bot.runtime import (
    callback_feedback,
    check_proxy_available,
    show_recovery_status,
)
from guardbot.config import logger
from guardbot.database import AsyncSessionLocal
from guardbot.services.ownership import get_owned_mafile
from guardbot.services.session_manager import SteamSessionManager

router = Router(name=__name__)


@router.callback_query(F.data.startswith("confirmations_menu_"))
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

@router.callback_query(F.data.startswith("confirm_single_"))
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

@router.callback_query(F.data.startswith("cancel_single_"))
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

@router.callback_query(F.data.startswith("confirm_all_"))
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


@router.callback_query(F.data.startswith("cancel_all_"))
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
