"""Extracted from the legacy bot module without behavior changes."""

from datetime import datetime
from guardbot.config import STEAM_CURRENCIES, logger
from guardbot.database import Mafile, User
from guardbot.security import decrypt_dict, decrypt_value, encrypt_dict, encrypt_value
from guardbot.steam.client import AsyncSteamMobile, parse_proxy_string
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# ==================== STEAM SESSION MANAGER ====================

class SteamSessionManager:
    """Менеджер для работы со Steam сессиями через БД"""

    @staticmethod
    async def create_steam_client_from_db(mafile: Mafile, db_session: AsyncSession) -> AsyncSteamMobile:
        """Создает Steam клиент из данных в БД (с расшифровкой)"""

        # 🔥 Расшифровываем пароль
        password = decrypt_value(mafile.password) if mafile.password else ""

        # Определяем какой прокси использовать
        proxy = None

        if mafile.unique_proxy and mafile.proxy:
            # 🔥 Расшифровываем уникальный прокси
            decrypted_proxy = decrypt_value(mafile.proxy)
            proxy = parse_proxy_string(decrypted_proxy) if decrypted_proxy else None
            logger.info(f"Using UNIQUE proxy for {mafile.account_name}")
        else:
            # Используем общий прокси пользователя
            stmt = select(User).where(User.telegram_id == mafile.telegram_id)
            result = await db_session.execute(stmt)
            user = result.scalar_one_or_none()

            if user and user.general_proxy:
                # 🔥 Расшифровываем общий прокси
                decrypted_proxy = decrypt_value(user.general_proxy)
                proxy = parse_proxy_string(decrypted_proxy) if decrypted_proxy else None
                logger.info(f"Using GENERAL proxy for {mafile.account_name}")
            else:
                # Проверяем, есть ли у пользователя вообще какие-то прокси
                stmt = select(Mafile).where(
                    Mafile.telegram_id == mafile.telegram_id,
                    Mafile.unique_proxy == True,
                    Mafile.proxy.isnot(None)
                )
                result = await db_session.execute(stmt)
                has_unique_proxy = result.scalar_one_or_none() is not None

                if has_unique_proxy:
                    raise Exception(f"Для аккаунта {mafile.account_name} не настроен прокси.\n"
                                f"Используйте /set_account_proxy {mafile.account_name} [url]")
                else:
                    raise Exception(f"Прокси не настроен. Используйте /set_proxy [url] для общего прокси\n"
                                f"или /set_account_proxy {mafile.account_name} [url] для уникального.")

        # Создаем клиент с расшифрованным паролем и прокси
        client = AsyncSteamMobile(mafile.account_name, password, proxy=proxy)

        # 🔥 Загружаем сессию если есть (расшифровывая)
        if mafile.session_data_encrypted:
            session_data = decrypt_dict(mafile.session_data_encrypted)
            if session_data:
                await client.load(session_data)

        # 🔥 Загружаем мобильные данные (расшифровывая)
        if mafile.shared_secret:
            mobile_data = {
                'device_id': decrypt_value(mafile.device_id),
                'shared_secret': decrypt_value(mafile.shared_secret),
                'serial_number': decrypt_value(mafile.serial_number),
                'revocation_code': decrypt_value(mafile.revocation_code),
                'uri': decrypt_value(mafile.uri),
                'token_gid': decrypt_value(mafile.token_gid),
                'identity_secret': decrypt_value(mafile.identity_secret),
                'secret_1': decrypt_value(mafile.secret_1)
            }
            client.load_mobile(mobile_data)

        # 🔥 Загружаем расшифрованные токены в клиент
        if mafile.access_token:
            client.access_token = decrypt_value(mafile.access_token)
        if mafile.refresh_token:
            client.refresh_token = decrypt_value(mafile.refresh_token)

        return client

    @staticmethod
    async def save_steam_session_to_db(session: AsyncSession, mafile_id: int, client: AsyncSteamMobile):
        """Сохраняет данные Steam сессии в БД"""
        stmt = select(Mafile).where(Mafile.id == mafile_id)
        result = await session.execute(stmt)
        mafile = result.scalar_one_or_none()

        if not mafile:
            logger.error(f"❌ Mafile not found for id={mafile_id}")
            return

        logger.info(f"💾 Starting save_steam_session_to_db for {mafile.account_name}")
        logger.info(f"   Current DB state: country={mafile.country}, currency_id={mafile.currency_id}")
        logger.info(f"   Client state: steamid={client.steamid}, has_access_token={bool(client.access_token)}, has_refresh_token={bool(client.refresh_token)}")

        # Конвертируем steamid в int если это строка
        if client.steamid:
            try:
                mafile.steamid = int(client.steamid)
            except (ValueError, TypeError):
                mafile.steamid = None
        else:
            mafile.steamid = None

        # 🔥 ШИФРУЕМ чувствительные поля перед сохранением
        mafile.access_token = encrypt_value(client.access_token)
        mafile.refresh_token = encrypt_value(client.refresh_token)
        mafile.device_id = encrypt_value(client.device_id)
        mafile.shared_secret = encrypt_value(client.shared_secret)
        mafile.identity_secret = encrypt_value(client.identity_secret)
        mafile.secret_1 = encrypt_value(client.secret_1)
        mafile.serial_number = encrypt_value(client.serial_number)
        mafile.revocation_code = encrypt_value(client.revocation_code)
        mafile.token_gid = encrypt_value(client.token_gid)
        mafile.uri = encrypt_value(client.uri)

        # JSON поля шифруем
        mafile.session_data_encrypted = encrypt_dict(client.export())
        mafile.cookies_encrypted = encrypt_dict(client.export_cookies_dict())

        mafile.last_login_at = datetime.utcnow()
        mafile.updated_at = datetime.utcnow()

        if client.shared_secret:
            mafile.fully_enrolled = True

        if client.refresh_token:
            try:
                expire_timestamp = client.get_token_expire_timestamp(client.refresh_token)
                mafile.token_expires_at = datetime.fromtimestamp(expire_timestamp)
            except:
                pass

        # 🔥 ВСЕГДА пытаемся определить страну (даже если уже есть — для обновления)
        logger.info(f"🌍 Attempting to detect country for {mafile.account_name}...")
        logger.info(f"   Current steamid for profile URL: {client.steamid}")

        try:
            country = await client.get_account_country()
            logger.info(f"🌍 get_account_country() returned: '{country}'")

            if country:
                old_country = mafile.country
                old_currency = mafile.currency_id

                mafile.country = country

                # Определяем currency_id по стране
                currency_found = False
                for curr_id, info in STEAM_CURRENCIES.items():
                    if info['country'] == country:
                        mafile.currency_id = curr_id
                        logger.info(f"💱 Currency set: {info['name']} (ID: {curr_id}) for {mafile.account_name}")
                        currency_found = True
                        break

                if not currency_found:
                    logger.warning(f"⚠️ No currency found for country '{country}'")

                if old_country != country or old_currency != mafile.currency_id:
                    logger.info(f"🔄 Country/currency changed: {old_country}/{old_currency} -> {country}/{mafile.currency_id}")
                else:
                    logger.info(f"✅ Country/currency unchanged: {country}/{mafile.currency_id}")
            else:
                logger.warning(f"❌ get_account_country() returned None for {mafile.account_name}")
                logger.warning(f"   Check if profile page is accessible: https://steamcommunity.com/profiles/{client.steamid}/")
        except Exception as e:
            logger.error(f"❌ Exception in get_account_country: {type(e).__name__}: {e}", exc_info=True)

        # Финальный лог перед коммитом
        logger.info(f"💾 Final state before commit for {mafile.account_name}:")
        logger.info(f"   steamid={mafile.steamid}")
        logger.info(f"   country={mafile.country}")
        logger.info(f"   currency_id={mafile.currency_id}")
        logger.info(f"   access_token={'yes' if mafile.access_token else 'no'}")
        logger.info(f"   refresh_token={'yes' if mafile.refresh_token else 'no'}")
        logger.info(f"   session_data={'yes' if mafile.session_data_encrypted else 'no'}")
        logger.info(f"   cookies={'yes' if mafile.cookies_encrypted else 'no'}")

        await session.commit()
        logger.info(f"✅ Steam session COMMITTED for account {mafile.account_name}")
