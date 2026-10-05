"""Session recovery and validity operations for Steam mobile clients."""

from guardbot.config import logger
from guardbot.steam.constants import USER_AGENT_MOBILE


class SessionRecoveryMixin:
    async def _full_session_recovery(self) -> bool:
        """
        Полное восстановление сессии: обновляет access_token через refresh_token,
        получает новый sessionid и проверяет что сессия работает.
        Возвращает True если удалось, False если нужен полный логин.
        """
        await self._ensure_session()

        logger.info(f"🔄 [{self.account_name}] Starting FULL session recovery...")

        if not self.refresh_token:
            logger.warning(f"❌ [{self.account_name}] No refresh_token, cannot recover")
            return False

        if self.is_token_expired(self.refresh_token):
            logger.warning(f"❌ [{self.account_name}] Refresh token expired, cannot recover")
            return False

        logger.info(f"🔄 [{self.account_name}] Refreshing access_token via refresh_token...")
        try:
            await self.refresh_access_token()
            logger.info(f"✅ [{self.account_name}] access_token refreshed successfully!")
        except Exception as e:
            logger.error(f"❌ [{self.account_name}] Failed to refresh access_token: {e}")
            return False

        logger.info(f"🔄 [{self.account_name}] Getting new sessionid with fresh access_token...")
        try:
            self.session_id = None
            old_cookies = []
            for cookie in self.cookie_jar:
                if cookie.key == 'sessionid':
                    old_cookies.append(cookie)
            for cookie in old_cookies:
                self.cookie_jar._cookies.pop(cookie.key, None)

            new_sessionid = await self._ensure_sessionid()
            logger.info(f"✅ [{self.account_name}] New sessionid obtained")
        except Exception as e:
            logger.error(f"❌ [{self.account_name}] Failed to get new sessionid: {e}")
            return False

        logger.info(f"🔄 [{self.account_name}] Verifying session works...")
        try:
            headers = {
                'User-Agent': USER_AGENT_MOBILE,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            }
            async with self.session.get('https://steamcommunity.com/my/', headers=headers, allow_redirects=False) as resp:
                logger.info(f"   Status: {resp.status}")

                if resp.status in [301, 302, 303, 307, 308]:
                    location = resp.headers.get('Location', '')
                    logger.warning(f"   Redirect to: {location}")
                    if 'login' in location.lower():
                        logger.error(f"❌ [{self.account_name}] Redirected to login — session still invalid!")
                        return False

                text = await resp.text()
                if 'login' in text.lower() and 'password' in text.lower():
                    logger.error(f"❌ [{self.account_name}] Got login page — session still invalid!")
                    return False

                logger.info(f"✅ [{self.account_name}] Session verified! /my/ accessible!")
                return True

        except Exception as e:
            logger.error(f"❌ [{self.account_name}] Session verification failed: {e}")
            return False

    async def _try_recover_session(self) -> bool:
        """
        Пытается восстановить сессию через полное обновление токенов.
        Возвращает True если удалось, False если нужен полный логин.
        """
        logger.info(f"🔄 [{self.account_name}] Attempting session recovery...")

        if not self.refresh_token:
            logger.warning(f"❌ [{self.account_name}] No refresh_token, cannot recover")
            return False

        if self.is_token_expired(self.refresh_token):
            logger.warning(f"❌ [{self.account_name}] Refresh token expired, cannot recover")
            return False

        return await self._full_session_recovery()

    async def check_session_valid(self) -> bool:
        """Проверяет, валидна ли текущая сессия"""
        await self._ensure_session()

        try:
            async with self.session.get('https://steamcommunity.com/my/') as resp:
                text = await resp.text()

                # Если редиректит на логин - сессия невалидна
                if 'login' in text.lower() and 'password' in text.lower():
                    return False

                return True
        except:
            return False

    async def ensure_valid_session(self) -> bool:
        """Гарантирует валидную сессию, обновляя её при необходимости"""
        if await self.check_session_valid():
            return True

        # Пробуем обновить через refresh token
        if self.refresh_token and not self.is_token_expired(self.refresh_token):
            try:
                await self.refresh_access_token()
                await self._ensure_sessionid()
                return await self.check_session_valid()
            except:
                pass

        return False
