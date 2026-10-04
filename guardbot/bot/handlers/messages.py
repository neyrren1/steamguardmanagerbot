"""Extracted from the legacy bot module without behavior changes."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from datetime import datetime
from guardbot.bot.handlers.accounts import perform_login_full, update_account_settings_message
from guardbot.bot.handlers.bulk_import import handle_mafile_for_account, process_bulk_import, process_steam_code, process_txt_accounts, process_zip_archive
from guardbot.bot.handlers.common import cmd_start_help
from guardbot.bot.handlers.imports import process_single_mafile
from guardbot.bot.runtime import bot, calculate_steam_price, dp, start_trade_check_task, user_states
from guardbot.config import logger
from guardbot.database import AccountGroup, AsyncSessionLocal, Mafile, User
from guardbot.passwords import (
    MIN_BOT_PASSWORD_LENGTH,
    PasswordRateLimitError,
    hash_bot_password_async,
    password_hash_needs_upgrade,
    verify_bot_password_async,
)
from guardbot.security import decrypt_value, encrypt_value
from guardbot.services.ownership import get_owned_group, get_owned_mafile
from guardbot.services.session_manager import SteamSessionManager
from guardbot.steam.client import AsyncSteamMobile, generate_device_id, parse_proxy_string, redact_proxy_url
from sqlalchemy import func, select, text
import io
import json

# ==================== ОБРАБОТЧИК МЕСЕДЖЕЙ ====================

@dp.message()
async def handle_messages(message: Message):
    """Обработчик всех текстовых сообщений в зависимости от состояния пользователя"""
    user_id = message.from_user.id
    state_data = user_states.get(user_id, {})
    state = state_data.get("state")

    # ========== СОСТОЯНИЕ: ОЖИДАНИЕ ДАННЫХ ДЛЯ ДОБАВЛЕНИЯ АККАУНТА ==========
    if state == "waiting_steam_credentials":
        try:
            text = message.text.strip()

            # 🔥 Удаляем сообщение с логином и паролем
            try:
                await message.delete()
            except:
                pass

            if ":" not in text:
                await message.answer("❌ Неверный формат. Используйте: account_name:password")
                return

            account_name, password = text.split(":", 1)

            async with AsyncSessionLocal() as session:
                stmt = select(Mafile).where(
                    Mafile.telegram_id == user_id,
                    Mafile.account_name == account_name
                )
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()

                if existing:
                    await message.answer(
                        f"❌ Аккаунт <b>{account_name}</b> уже существует в вашей базе",
                        parse_mode="HTML"
                    )
                    return

                # Получаем максимальный sort_order
                stmt = select(func.max(Mafile.sort_order)).where(Mafile.telegram_id == user_id)
                result = await session.execute(stmt)
                max_order = result.scalar() or 0

                mafile = Mafile(
                    telegram_id=user_id,
                    account_name=account_name,
                    password=encrypt_value(password),
                    unique_proxy=False,
                    sort_order=max_order + 1
                )

                session.add(mafile)
                await session.commit()

                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(text="🔑 Войти по паролю", callback_data=f"action_login_{mafile_id}"),
                            InlineKeyboardButton(text="🔄 Войти по токену", callback_data=f"try_refresh_{mafile_id}")
                        ],
                        [InlineKeyboardButton(
                            text="👤 К аккаунту",
                            callback_data=f"account_detail_{mafile.id}"
                        )]
                    ]
                )

                await message.answer(
                    f"✅ <b>Аккаунт {account_name} добавлен!</b>\n\n"
                    f"<b>Что дальше?</b>\n\n"
                    f"Нажмите <b>«Войти в аккаунт»</b> для авторизации.\n\n"
                    f"<i>💡 Бот сам определит, есть ли Steam Guard, и подскажет что делать.</i>\n\n"
                    f"Команда для входа: <code>/login {account_name}</code>",
                    parse_mode="HTML",
                    reply_markup=keyboard
                )

        except Exception as e:
            await message.answer(f"❌ Ошибка при добавлении аккаунта: {str(e)}")
            logger.error(f"Error adding account for user {user_id}: {e}")
        finally:
            user_states.pop(user_id, None)

     # ========== СОСТОЯНИЕ: ОЖИДАНИЕ ДЛЯ НАСТРОЙКИ МОНИТОРНИНГА ==========

# В функции handle_messages, после других состояний:
    elif state == "waiting_trade_notify_interval":
        try:

            interval = int(message.text.strip())
            if interval < 1:
                await message.answer("❌ Интервал должен быть не менее 1 минуты")
                return

            mafile_id = state_data.get("mafile_id")
            is_update = state_data.get("is_update", False)

            async with AsyncSessionLocal() as session:
                mafile = await get_owned_mafile(session, mafile_id, message.from_user.id)
                if not mafile:
                    await message.answer("❌ Аккаунт не найден")
                    user_states.pop(user_id, None)
                    return

                mafile.trade_notifications = True
                mafile.trade_notify_interval = interval
                mafile.known_trade_ids = []
                await session.commit()

                # Запускаем задачу проверки
                start_trade_check_task(mafile_id)

                action = "обновлены" if is_update else "включены"

                # Создаем клавиатуру для возврата
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="◀️ К настройкам уведомлений",
                            callback_data=f"trade_notifications_menu_{mafile_id}"
                        )]
                    ]
                )

                await message.answer(
                    f"✅ <b>Уведомления {action}!</b>\n\n"
                    f"📱 Аккаунт: <code>{mafile.account_name}</code>\n"
                    f"⏱ Интервал проверки: каждые {interval} мин\n\n"
                    f"Бот будет проверять новые трейды и присылать уведомления.",
                    parse_mode="HTML",
                    reply_markup=keyboard
                )

        except ValueError:
            await message.answer("❌ Введите целое число (минуты)")
            return
        finally:
            user_states.pop(user_id, None)

    # ========== СОСТОЯНИЕ: ОЖИДАНИЕ ПОЛЬЗОВАТЕЛЬСКОЙ ЦЕНЫ ==========
    elif state == "waiting_custom_price":
        try:
            price_text = message.text.strip().replace(',', '.')
            price_float = float(price_text)

            if price_float <= 0:
                await message.answer("❌ Цена должна быть больше 0")
                return

            mafile_id = state_data.get("mafile_id")
            app_id = state_data.get("app_id")
            assetid = state_data.get("assetid")
            context_id = state_data.get("context_id", 2)

            async with AsyncSessionLocal() as session:
                mafile = await get_owned_mafile(session, mafile_id, message.from_user.id)
                if not mafile:
                    await message.answer("❌ Аккаунт не найден")
                    user_states.pop(user_id, None)
                    return

                # 🔥 Получаем currency_id из БД
                currency = mafile.currency_id or 18

                # Конвертируем в копейки/центы и вычитаем комиссию 3/23%
                price_with_commission = calculate_steam_price(price_float, currency)

                if price_with_commission <= 0:
                    await message.answer("❌ Цена слишком низкая после комиссии")
                    user_states.pop(user_id, None)
                    return

                client = await SteamSessionManager.create_steam_client_from_db(mafile, session)

                try:
                    status_msg = await message.answer(
                        f"💰 <b>Продаю предмет за {price_float}...</b>\n"
                        f"<i>Цена после комиссии (3/23): {price_with_commission/100:.2f}</i>",
                        parse_mode="HTML"
                    )

                    result = await client.sell_item_with_recovery(app_id, context_id, assetid, 1, price_with_commission)

                    if result.get('success'):
                        if result.get('needs_mobile_confirmation'):
                            text = (
                                f"✅ <b>Предмет выставлен на продажу!</b>\n\n"
                                f"💵 <b>Цена:</b> {price_float}\n\n"
                                f"⚠️ <b>Требуется подтверждение в Steam Guard</b>"
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
                        else:
                            text = f"✅ <b>Предмет успешно выставлен на продажу за {price_float}!</b>"
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
                            f"❌ <b>Не удалось продать предмет</b>\n\n<code>{error[:300]}</code>",
                            parse_mode="HTML"
                        )

                finally:
                    await client.close()

        except ValueError:
            await message.answer("❌ Введите корректное число (например: 8.70)")
            return

        user_states.pop(user_id, None)
    # ========== СОСТОЯНИЕ: ОЖИДАНИЕ КОДА ПОДТВЕРЖДЕНИЯ STEAM ==========
    # Замените состояние waiting_steam_code на:
    elif state == "waiting_steam_code":
        # Обработка кода когда он был сгенерирован ботом
        await process_steam_code(message, state_data, user_id)

    elif state == "waiting_steam_code_manual":
        # Обработка кода введенного вручную
        await process_steam_code(message, state_data, user_id)

    elif state == "waiting_email_code":
        # Обработка кода с почты
        await process_steam_code(message, state_data, user_id)
    # В обработка массового импорта акков
    elif state == "waiting_bulk_credentials":
        await process_bulk_import(message, user_id)

    # ========== СОСТОЯНИЕ: ОЖИДАНИЕ ПАРОЛЯ ДЛЯ ВХОДА ==========
    elif state == "waiting_password_for_login":
        password = message.text.strip()
        mafile_id = state_data.get("mafile_id")
        account_name = state_data.get("account_name")

        try:
            await message.delete()
        except:
            pass

        async with AsyncSessionLocal() as session:
            mafile = await get_owned_mafile(session, mafile_id, message.from_user.id)

            if not mafile:
                await message.answer("❌ Аккаунт не найден")
                user_states.pop(user_id, None)
                return

            # 🔥 Шифруем пароль перед сохранением
            mafile.password = encrypt_value(password)
            await session.commit()

            await message.answer(
                f"✅ <b>Пароль сохранен!</b>\n\n"
                f"Пробую войти в аккаунт...",
                parse_mode="HTML"
            )

            # Пробуем войти
            await perform_login_full(message, mafile, session)

        user_states.pop(user_id, None)

    # ========== СОСТОЯНИЕ: ОЖИДАНИЕ ПАРОЛЯ ПРИ ИМПОРТЕ MAFILE ==========
    elif state == "waiting_password_for_mafile":
        password = message.text.strip()
        mafile_data = state_data.get("mafile_data")

        if not mafile_data:
            await message.answer("❌ Данные mafile потеряны. Попробуйте импортировать заново.")
            user_states.pop(user_id, None)
            return

        try:
            # Удаляем сообщение с паролем для безопасности
            try:
                await message.delete()
            except:
                pass

            async with AsyncSessionLocal() as session:
                # Проверяем, существует ли уже такой аккаунт
                stmt = select(Mafile).where(
                    Mafile.telegram_id == user_id,
                    Mafile.account_name == mafile_data['account_name']
                )
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()

                if existing:
                    await message.answer(
                        f"❌ Аккаунт <b>{mafile_data['account_name']}</b> уже существует!\n"
                        f"Используйте /accounts для просмотра или /delete для удаления.",
                        parse_mode="HTML"
                    )
                    user_states.pop(user_id, None)
                    return

                stmt = select(func.max(Mafile.sort_order)).where(Mafile.telegram_id == user_id)
                result = await session.execute(stmt)
                max_order = result.scalar() or 0

                # 🔥 Создаем новый аккаунт с шифрованием всех чувствительных данных
                mafile = Mafile(
                    telegram_id=user_id,
                    account_name=mafile_data['account_name'],
                    password=encrypt_value(password),
                    shared_secret=encrypt_value(mafile_data.get('shared_secret')),
                    identity_secret=encrypt_value(mafile_data.get('identity_secret')),
                    secret_1=encrypt_value(mafile_data.get('secret_1')),
                    device_id=encrypt_value(mafile_data.get('device_id')),
                    serial_number=encrypt_value(mafile_data.get('serial_number')),
                    revocation_code=encrypt_value(mafile_data.get('revocation_code')),
                    token_gid=encrypt_value(mafile_data.get('token_gid')),
                    uri=encrypt_value(mafile_data.get('uri')),
                    steamid=mafile_data.get('steamid'),
                    fully_enrolled=True,
                    unique_proxy=False,
                    sort_order=max_order + 1
                )

                # Шифруем токены если есть
                if 'Session' in mafile_data:
                    mafile.access_token = encrypt_value(mafile_data['Session'].get('AccessToken'))
                    mafile.refresh_token = encrypt_value(mafile_data['Session'].get('RefreshToken'))

                session.add(mafile)
                await session.commit()

                # Проверяем, генерируется ли код
                test_client = AsyncSteamMobile(mafile_data['account_name'], password)
                test_client.load_mobile({
                    'shared_secret': mafile_data.get('shared_secret'),
                    'device_id': mafile_data.get('device_id', generate_device_id()),
                    'identity_secret': mafile_data.get('identity_secret'),
                    'serial_number': mafile_data.get('serial_number'),
                    'revocation_code': mafile_data.get('revocation_code'),
                    'uri': mafile_data.get('uri'),
                    'token_gid': mafile_data.get('token_gid'),
                    'secret_1': mafile_data.get('secret_1')
                })

                try:
                    await test_client.align_time()
                    test_code = test_client.generate_steam_guard_code()

                    await message.answer(
                        f"✅ <b>Аккаунт успешно импортирован!</b>\n\n"
                        f"📱 <b>Логин:</b> <code>{mafile_data['account_name']}</code>\n"
                        f"🆔 <b>SteamID:</b> <code>{mafile_data.get('steamid', 'Не указан')}</code>\n"
                        f"🔐 <b>Тестовый код:</b> <b><code>{test_code}</code></b>\n\n"
                        f"<b>Доступные команды:</b>\n"
                        f"• <code>/gc {mafile_data['account_name']}</code> - получить код\n"
                        f"• <code>/login {mafile_data['account_name']}</code> - войти в аккаунт\n"
                        f"• <code>/accounts</code> - список всех аккаунтов\n\n"
                        f"💡 <b>Важно:</b> Если есть refresh_token в mafile, выполните вход для обновления токенов.",
                        parse_mode="HTML"
                    )

                except Exception as e:
                    await message.answer(
                        f"⚠️ Аккаунт сохранен, но есть проблема с генерацией кода:\n"
                        f"<code>{str(e)}</code>\n\n"
                        f"Попробуйте выполнить вход: <code>/login {mafile_data['account_name']}</code>",
                        parse_mode="HTML"
                    )
                finally:
                    await test_client.close()

        except Exception as e:
            await message.answer(f"❌ Ошибка при сохранении: {str(e)}")
            logger.error(f"Error saving imported mafile for user {user_id}: {e}")
        finally:
            user_states.pop(user_id, None)
    # ========== СОСТОЯНИЕ: ОЖИДАНИЕ SHARED_SECRET ==========
    elif state == "waiting_shared_secret":
        shared_secret = message.text.strip()
        account_name = state_data.get("account_name")

        try:
            # Удаляем сообщение с секретом для безопасности
            try:
                await message.delete()
            except:
                pass

            async with AsyncSessionLocal() as session:
                stmt = select(Mafile).where(
                    Mafile.telegram_id == user_id,
                    Mafile.account_name == account_name
                )
                result = await session.execute(stmt)
                mafile = result.scalar_one_or_none()

                if not mafile:
                    await message.answer(f"❌ Аккаунт <b>{account_name}</b> не найден", parse_mode="HTML")
                    user_states.pop(user_id, None)
                    return

                mafile.shared_secret = encrypt_value(shared_secret)
                mafile.fully_enrolled = True
                await session.commit()

                # Проверяем генерацию кода
                client = AsyncSteamMobile(account_name, mafile.password)
                client.load_mobile({
                    'shared_secret': shared_secret,
                    'device_id': mafile.device_id or generate_device_id()
                })

                try:
                    await client.align_time()
                    code = client.generate_steam_guard_code()

                    await message.answer(
                        f"✅ <b>Shared secret установлен!</b>\n\n"
                        f"📱 Аккаунт: <code>{account_name}</code>\n"
                        f"🔐 Тестовый код: <b><code>{code}</code></b>\n\n"
                        f"Теперь можете использовать <code>/gc {account_name}</code>",
                        parse_mode="HTML"
                    )
                except Exception as e:
                    await message.answer(
                        f"⚠️ Shared secret сохранен, но есть проблема с генерацией кода:\n"
                        f"<code>{str(e)}</code>\n\n"
                        f"Возможно потребуется также установить device_id через /set_device_id",
                        parse_mode="HTML"
                    )
                finally:
                    await client.close()

        except Exception as e:
            await message.answer(f"❌ Ошибка при сохранении shared_secret: {str(e)}")
            logger.error(f"Error saving shared_secret: {e}")
        finally:
            user_states.pop(user_id, None)

    # ========== СОСТОЯНИЕ: ОЖИДАНИЕ ЗАМЕТКИ ==========

    elif state == "waiting_note":
        note_text = message.text.strip()
        mafile_id = state_data.get("mafile_id")
        account_name = state_data.get("account_name")

        # 🔥 Удаляем сообщение с заметкой (опционально)
        try:
            await message.delete()
        except:
            pass

        async with AsyncSessionLocal() as session:
            mafile = await get_owned_mafile(session, mafile_id, message.from_user.id)
            if not mafile:
                await message.answer("❌ Аккаунт не найден")
                user_states.pop(user_id, None)
                return

            # Проверяем, не точка ли это (удаление заметки)
            if note_text == ".":
                mafile.notes = None
                await session.commit()

                action = "удалена"
                note_value = ""
            else:
                # Ограничиваем длину заметки
                if len(note_text) > 500:
                    note_text = note_text[:500]

                mafile.notes = encrypt_value(note_text)
                await session.commit()

                action = "сохранена"
                note_value = f"\n📝 Заметка: <i>{note_text}</i>"

            # Отправляем новое сообщение с обновленной клавиатурой
            settings_msg = await message.answer(
                "⚙️ <b>Загружаю настройки...</b>",
                parse_mode="HTML"
            )
            await update_account_settings_message(settings_msg, mafile, session)

            await message.answer(
                f"✅ <b>Заметка {action}!</b>\n\n"
                f"📱 Аккаунт: <code>{account_name}</code>{note_value}",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="👤 К настройкам аккаунта",
                            callback_data=f"account_settings_{mafile.id}"
                        )]
                    ]
                )
            )

        user_states.pop(user_id, None)

    # ========== СОСТОЯНИЕ: ОЖИДАНИЕ УНИКАЛЬНОГО ПРОКСИ ==========

    elif state == "waiting_unique_proxy":
        proxy_input = message.text.strip()
        mafile_id = state_data.get("mafile_id")
        account_name = state_data.get("account_name")

        async with AsyncSessionLocal() as session:
            mafile = await get_owned_mafile(session, mafile_id, message.from_user.id)
            if not mafile:
                await message.answer("❌ Аккаунт не найден")
                user_states.pop(user_id, None)
                return

            # Удаляем сообщение пользователя для безопасности
            try:
                await message.delete()
            except:
                pass

            # Проверяем, не точка ли это (удаление прокси)
            if proxy_input == ".":
                mafile.proxy = None
                mafile.unique_proxy = False
                await session.commit()

                # 🔥 Отправляем новое сообщение с обновленной клавиатурой
                settings_msg = await message.answer(
                    "⚙️ <b>Загружаю настройки...</b>",
                    parse_mode="HTML"
                )
                await update_account_settings_message(settings_msg, mafile, session)

                await message.answer(
                    f"✅ <b>Уникальный прокси удален!</b>\n\n"
                    f"📱 Аккаунт: <code>{account_name}</code>\n"
                    f"Теперь используется общий прокси.",
                    parse_mode="HTML"
                )

                user_states.pop(user_id, None)
                return

            try:
                proxy_url = parse_proxy_string(proxy_input)
            except ValueError:
                await message.answer(
                    "❌ <b>Некорректный или небезопасный адрес прокси.</b>\n\n"
                    "Попробуйте снова или нажмите кнопку ниже:",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="🔄 Попробовать снова",
                                callback_data=f"change_unique_proxy_{mafile.id}"
                            )],
                            [InlineKeyboardButton(
                                text="❌ Отмена",
                                callback_data=f"account_settings_{mafile.id}"
                            )]
                        ]
                    )
                )
                return

            # Сохраняем прокси
            old_proxy = mafile.proxy
            mafile.proxy = encrypt_value(proxy_url)
            await session.commit()

            action = "изменен" if old_proxy else "добавлен"

            # 🔥 Отправляем новое сообщение с обновленной клавиатурой
            settings_msg = await message.answer(
                "⚙️ <b>Загружаю настройки...</b>",
                parse_mode="HTML"
            )
            await update_account_settings_message(settings_msg, mafile, session)

            await message.answer(
                f"✅ <b>Уникальный прокси {action}!</b>\n\n"
                f"📱 Аккаунт: <code>{account_name}</code>\n"
                f"🌐 Прокси: <code>{redact_proxy_url(proxy_url)}</code>\n\n"
                f"<i>Нажмите кнопку ниже, чтобы включить уникальный прокси</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(
                            text="🟢 Включить уникальный прокси",
                            callback_data=f"toggle_unique_proxy_{mafile.id}_on"
                        )],
                        [InlineKeyboardButton(
                            text="👤 К настройкам аккаунта",
                            callback_data=f"account_settings_{mafile.id}"
                        )]
                    ]
                )
            )

        user_states.pop(user_id, None)

    # ========== СОСТОЯНИЕ: ОЖИДАНИЕ DEVICE_ID ==========
    elif state == "waiting_device_id":
        device_id = message.text.strip()
        account_name = state_data.get("account_name")

        # 🔥 Удаляем сообщение с device_id
        try:
            await message.delete()
        except:
            pass

        try:
            async with AsyncSessionLocal() as session:
                stmt = select(Mafile).where(
                    Mafile.telegram_id == user_id,
                    Mafile.account_name == account_name
                )
                result = await session.execute(stmt)
                mafile = result.scalar_one_or_none()

                if not mafile:
                    await message.answer(f"❌ Аккаунт <b>{account_name}</b> не найден", parse_mode="HTML")
                    user_states.pop(user_id, None)
                    return

                mafile.device_id = encrypt_value(device_id)
                await session.commit()

                await message.answer(
                    f"✅ Device ID установлен для аккаунта <b>{account_name}</b>\n"
                    f"Device ID: <code>{device_id}</code>",
                    parse_mode="HTML"
                )

        except Exception as e:
            await message.answer(f"❌ Ошибка при сохранении device_id: {str(e)}")
        finally:
            user_states.pop(user_id, None)

    # ========== СОСТОЯНИЕ: ОЖИДАНИЕ IDENTITY_SECRET ==========
    elif state == "waiting_identity_secret":
        identity_secret = message.text.strip()
        account_name = state_data.get("account_name")

        try:
            # Удаляем сообщение с секретом для безопасности
            try:
                await message.delete()
            except:
                pass

            async with AsyncSessionLocal() as session:
                stmt = select(Mafile).where(
                    Mafile.telegram_id == user_id,
                    Mafile.account_name == account_name
                )
                result = await session.execute(stmt)
                mafile = result.scalar_one_or_none()

                if not mafile:
                    await message.answer(f"❌ Аккаунт <b>{account_name}</b> не найден", parse_mode="HTML")
                    user_states.pop(user_id, None)
                    return

                mafile.identity_secret = encrypt_value(identity_secret)
                await session.commit()

                await message.answer(
                    f"✅ Identity secret установлен для аккаунта <b>{account_name}</b>",
                    parse_mode="HTML"
                )

        except Exception as e:
            await message.answer(f"❌ Ошибка при сохранении identity_secret: {str(e)}")
        finally:
            user_states.pop(user_id, None)

    # ========== СОСТОЯНИЕ: ОЖИДАНИЕ MAFILE ДЛЯ КОНКРЕТНОГО АККАУНТА ==========
    elif state == "waiting_mafile_for_account":
        if not message.document:
            await message.answer(
                "❌ Ожидается файл .mafile\n"
                "Отправьте файл или /cancel для отмены"
            )
            return

        # 🔥 Удаляем сообщение с файлом
        try:
            await message.delete()
        except:
            pass

        mafile_id = state_data.get("mafile_id")
        account_name = state_data.get("account_name")
        document = message.document

        if not document.file_name.lower().endswith('.mafile') and not document.file_name.lower().endswith('.json'):
            await message.answer(
                "⚠️ Файл должен иметь расширение .mafile или .json\n"
                "Попробуйте снова с правильным файлом."
            )
            return

        status_msg = await message.answer("🔄 Обрабатываю файл...")

        try:
            # Скачиваем файл
            file_id = document.file_id
            file = await bot.get_file(file_id)
            file_path = file.file_path

            import io
            file_bytes = io.BytesIO()
            await bot.download_file(file_path, file_bytes)
            file_bytes.seek(0)

            file_content = file_bytes.read().decode('utf-8')
            mafile_data = json.loads(file_content)

            # Проверяем что account_name совпадает
            if mafile_data.get('account_name') != account_name:
                await status_msg.edit_text(
                    f"❌ <b>Ошибка!</b>\n\n"
                    f"В файле указан аккаунт <code>{mafile_data.get('account_name')}</code>\n"
                    f"Но ожидается <code>{account_name}</code>\n\n"
                    f"Отправьте правильный mafile для этого аккаунта.",
                    parse_mode="HTML"
                )
                user_states.pop(user_id, None)
                return

            # Проверяем обязательные поля
            if 'shared_secret' not in mafile_data:
                await status_msg.edit_text(
                    "❌ В файле отсутствует обязательное поле shared_secret",
                    parse_mode="HTML"
                )
                user_states.pop(user_id, None)
                return

            async with AsyncSessionLocal() as session:
                mafile = await get_owned_mafile(session, mafile_id, message.from_user.id)

                if not mafile:
                    await status_msg.edit_text("❌ Аккаунт не найден в базе")
                    user_states.pop(user_id, None)
                    return

                # Обновляем данные
                mafile.shared_secret = encrypt_value(mafile_data.get('shared_secret'))
                mafile.identity_secret = encrypt_value(mafile_data.get('identity_secret'))
                mafile.secret_1 = encrypt_value(mafile_data.get('secret_1'))
                mafile.device_id = encrypt_value(mafile_data.get('device_id'))
                mafile.serial_number = encrypt_value(mafile_data.get('serial_number'))
                mafile.revocation_code = encrypt_value(mafile_data.get('revocation_code'))
                mafile.token_gid = encrypt_value(mafile_data.get('token_gid'))
                mafile.uri = encrypt_value(mafile_data.get('uri'))
                mafile.fully_enrolled = True
                mafile.updated_at = datetime.utcnow()

                await session.commit()

                # Проверяем генерацию кода
                client = AsyncSteamMobile(mafile.account_name, mafile.password)
                client.load_mobile({
                    'shared_secret': decrypt_value(mafile.shared_secret),
                    'device_id': decrypt_value(mafile.device_id) or generate_device_id()
                })

                try:
                    await client.align_time()
                    code = client.generate_steam_guard_code()

                    keyboard = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(
                                text="🔐 Получить код",
                                callback_data=f"action_get_code_{mafile.id}"
                            )],
                            [InlineKeyboardButton(
                                text="👤 К аккаунту",
                                callback_data=f"account_detail_{mafile.id}"
                            )]
                        ]
                    )

                    await status_msg.edit_text(
                        f"✅ <b>Steam Guard успешно импортирован!</b>\n\n"
                        f"📱 <b>Аккаунт:</b> <code>{account_name}</code>\n"
                        f"🔐 <b>Тестовый код:</b> <code>{code}</code>\n\n"
                        f"Теперь вы можете получать коды!",
                        parse_mode="HTML",
                        reply_markup=keyboard
                    )

                except Exception as e:
                    await status_msg.edit_text(
                        f"⚠️ <b>Данные сохранены, но есть проблема с генерацией кода:</b>\n"
                        f"<code>{str(e)}</code>\n\n"
                        f"Попробуйте выполнить вход для синхронизации.",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(
                                    text="🔑 Войти",
                                    callback_data=f"action_login_{mafile.id}"
                                )]
                            ]
                        )
                    )
                finally:
                    await client.close()

        except json.JSONDecodeError as e:
            await status_msg.edit_text(f"❌ Файл поврежден или имеет неверный формат JSON: {str(e)}")
        except UnicodeDecodeError:
            try:
                file_bytes.seek(0)
                file_content = file_bytes.read().decode('cp1251')
                mafile_data = json.loads(file_content)
                # ... можно добавить повторную обработку если нужно
                await status_msg.edit_text("❌ Неверная кодировка файла. Сохраните файл в UTF-8.")
            except:
                await status_msg.edit_text("❌ Не удалось прочитать файл")
        except Exception as e:
            await status_msg.edit_text(f"❌ Ошибка при импорте: {str(e)}")
            logger.error(f"Error importing mafile for account {account_name}: {e}")
        finally:
            user_states.pop(user_id, None)

    # ========== СОСТОЯНИЕ: ОЖИДАНИЕ КОДА ИЛИ MAFILE ==========
    elif state == "waiting_steam_code_or_mafile":
        # 🔥 Удаляем сообщение (файл или код)
        try:
            await message.delete()
        except:
            pass
        if message.document:
            # Если прислали файл - обрабатываем как mafile
            await handle_mafile_for_account(message, state_data, user_id)
        else:
            # Если прислали текст - обрабатываем как код
            await process_steam_code(message, state_data, user_id)

    # ========== ОБРАБОТКА ДОКУМЕНТОВ БЕЗ СОСТОЯНИЯ ==========
    elif message.document:
        document = message.document
        file_name_lower = document.file_name.lower()

        # Сначала проверяем ZIP
        if file_name_lower.endswith('.zip'):
            await process_zip_archive(message, user_id)
            return

        # Потом TXT с аккаунтами
        if file_name_lower.endswith('.txt'):
            await process_txt_accounts(message, user_id)
            return

        # Потом одиночный mafile/json
        if file_name_lower.endswith('.mafile') or file_name_lower.endswith('.json'):
            await process_single_mafile(message, user_id)
            return

    # ========== СОСТОЯНИЕ: ОЖИДАНИЕ ПАРОЛЯ ДЛЯ БОТА ==========
    elif state == "waiting_bot_password":
        password = message.text.strip()
        action = state_data.get("action")
        telegram_id = user_id

        try:
            await message.delete()
        except:
            pass

        async with AsyncSessionLocal() as session:
            stmt = select(User).where(User.telegram_id == telegram_id)
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()

            if action == "remove":
                # Проверяем текущий пароль
                if not user or not user.bot_password:
                    await message.answer("❌ Пароль не установлен")
                    user_states.pop(user_id, None)
                    return

                try:
                    password_valid = await verify_bot_password_async(
                        user.bot_password, password, user_id=telegram_id
                    )
                except PasswordRateLimitError:
                    await message.answer("❌ Слишком много попыток. Повторите через минуту.")
                    user_states.pop(user_id, None)
                    return
                if not password_valid:
                    await message.answer("❌ <b>Неверный пароль!</b>", parse_mode="HTML")
                    user_states.pop(user_id, None)
                    return

                user.bot_password = None
                await session.commit()
                await message.answer("✅ <b>Пароль удалён!</b>", parse_mode="HTML")
            else:
                # Установка или изменение пароля
                if len(password) < MIN_BOT_PASSWORD_LENGTH:
                    await message.answer(
                        f"❌ Пароль должен быть не менее {MIN_BOT_PASSWORD_LENGTH} символов"
                    )
                    return

                if not user:
                    user = User(
                        telegram_id=telegram_id,
                        username=message.from_user.username,
                        full_name=message.from_user.full_name,
                        bot_password=await hash_bot_password_async(password)
                    )
                    session.add(user)
                else:
                    user.bot_password = await hash_bot_password_async(password)

                await session.commit()

                action_text = "изменён" if action == "change" else "установлен"
                await message.answer(
                    f"✅ <b>Пароль {action_text}!</b>\n\n"
                    f"<code>/lock</code> — заблокировать бота\n"
                    f"<code>/unlock [пароль]</code> — разблокировать",
                    parse_mode="HTML"
                )

        user_states.pop(user_id, None)


    # ========== СОСТОЯНИЕ: ОЖИДАНИЕ ПАРОЛЯ ДЛЯ РАЗБЛОКИРОВКИ ==========
    elif state == "waiting_unlock_password":
        password = message.text.strip()
        telegram_id = user_id

        try:
            await message.delete()
        except:
            pass

        async with AsyncSessionLocal() as session:
            stmt = select(User).where(User.telegram_id == telegram_id)
            result = await session.execute(stmt)
            user = result.scalar_one_or_none()

            if not user or not user.bot_password:
                await message.answer("❌ Пароль не установлен")
                user_states.pop(user_id, None)
                return

            try:
                password_valid = await verify_bot_password_async(
                    user.bot_password, password, user_id=telegram_id
                )
            except PasswordRateLimitError:
                await message.answer("❌ Слишком много попыток. Повторите через минуту.")
                return
            if not password_valid:
                await message.answer("❌ <b>Неверный пароль!</b>", parse_mode="HTML")
                return

            if password_hash_needs_upgrade(user.bot_password):
                user.bot_password = await hash_bot_password_async(password)

            user.last_activity = datetime.utcnow()
            await session.commit()

        await message.answer("✅ <b>Бот разблокирован!</b>", parse_mode="HTML")
        user_states.pop(user_id, None)

        # 🔥 Автоматически показываем главное меню
        await cmd_start_help(message)

    # ========== СОСТОЯНИЕ: ОЖИДАНИЕ НАЗВАНИЯ ГРУППЫ ==========
    elif state == "waiting_group_name":
        group_name = message.text.strip()
        telegram_id = user_id

        try:
            await message.delete()
        except:
            pass

        async with AsyncSessionLocal() as session:
            stmt = select(func.max(AccountGroup.sort_order)).where(AccountGroup.telegram_id == telegram_id)
            result = await session.execute(stmt)
            max_order = result.scalar() or 0

            group = AccountGroup(
                telegram_id=telegram_id,
                name=group_name,
                sort_order=max_order + 1
            )
            session.add(group)
            await session.commit()

        await message.answer(
            f"✅ Группа <b>{group_name}</b> создана!\n"
            f"Используйте /groups для просмотра.",
            parse_mode="HTML"
        )
        user_states.pop(user_id, None)

    # ========== СОСТОЯНИЕ: ОЖИДАНИЕ ПЕРЕИМЕНОВАНИЯ ГРУППЫ ==========
    elif state == "waiting_group_rename":
        new_name = message.text.strip()
        group_id = state_data.get("group_id")
        telegram_id = user_id

        try:
            await message.delete()
        except:
            pass

        async with AsyncSessionLocal() as session:
            grp = await get_owned_group(session, group_id, message.from_user.id)
            if not grp:
                await message.answer("❌ Группа не найдена")
                user_states.pop(user_id, None)
                return

            old_name = grp.name
            grp.name = new_name
            await session.commit()

        await message.answer(
            f"✅ Группа переименована:\n"
            f"<b>{old_name}</b> → <b>{new_name}</b>",
            parse_mode="HTML"
        )
        user_states.pop(user_id, None)

    # ========== НЕТ АКТИВНОГО СОСТОЯНИЯ ==========
    else:
        # Если нет активного состояния, просто игнорируем сообщение
        # или можно отправить подсказку
        if message.text and not message.text.startswith('/'):
            await message.answer(
                "💡 Используйте команды для управления ботом:\n"
                "/start - главное меню\n"
                "/commands - список всех команд\n"
                "/accounts - список аккаунтов\n"
                "/add - добавить аккаунт\n"
                "/gc - получить Steam Guard код"
            )
