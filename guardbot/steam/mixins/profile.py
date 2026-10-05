"""Steam profile and community operations."""

from typing import Optional

from lxml import html

from guardbot.config import logger
from guardbot.steam.constants import USER_AGENT_MOBILE


class ProfileMixin:
    async def get_account_country(self) -> Optional[str]:
        """Получает страну аккаунта из HTML профиля"""
        import re

        await self._ensure_session()

        headers = {
            'User-Agent': USER_AGENT_MOBILE,
            'Accept': 'text/html',
        }

        try:
            url = 'https://steamcommunity.com/my/'
            logger.info(f"🌍 Fetching profile page for country: {url}")

            async with self.session.get(url, headers=headers, allow_redirects=True) as resp:
                if resp.status != 200:
                    logger.warning(f"❌ Profile page returned HTTP {resp.status}")
                    return None

                html_text = await resp.text()
                logger.info(f"🌍 Got HTML response, length: {len(html_text)}")

                # 🔥 Способ 1: Ищем в data-config (там COUNTRY в HTML entities)
                # Паттерн: &quot;COUNTRY&quot;:&quot;UA&quot;
                match = re.search(r'&quot;COUNTRY&quot;\s*:\s*&quot;([^&]+)&quot;', html_text)
                if match:
                    country = match.group(1)
                    logger.info(f"🌍 Account country detected (method 1 - data-config): {country}")
                    return country

                # 🔥 Способ 2: Ищем country_code в data-userinfo
                # Паттерн: &quot;country_code&quot;:&quot;UA&quot;
                match = re.search(r'&quot;country_code&quot;\s*:\s*&quot;([^&]+)&quot;', html_text)
                if match:
                    country = match.group(1)
                    logger.info(f"🌍 Account country detected (method 2 - data-userinfo): {country}")
                    return country

                # 🔥 Способ 3: Ищем обычные кавычки (на случай если где-то есть)
                match = re.search(r'"COUNTRY"\s*:\s*"([^"]+)"', html_text)
                if match:
                    country = match.group(1)
                    logger.info(f"🌍 Account country detected (method 3 - regular quotes): {country}")
                    return country

                match = re.search(r'"country_code"\s*:\s*"([^"]+)"', html_text)
                if match:
                    country = match.group(1)
                    logger.info(f"🌍 Account country detected (method 4 - regular quotes country_code): {country}")
                    return country

                logger.warning(f"❌ COUNTRY not found in HTML response for {self.account_name}")
                # Логируем кусок с data-config для отладки
                config_match = re.search(r'data-config="([^"]{200,})"', html_text)
                if config_match:
                    logger.info("📋 Profile data-config found")
                else:
                    # 🔥 Логируем первые 500 символов HTML чтобы понять что пришло
                    logger.warning("🌍 No data-config found in profile response")

        except Exception as e:
            logger.error(f"Failed to get account country: {e}", exc_info=True)

        return None

    async def get_tradelink(self) -> Optional[str]:
        """Получение трейд-ссылки"""
        await self._ensure_session()

        # Проверяем sessionid
        try:
            await self._ensure_sessionid()
        except:
            return None

        try:
            async with self.session.get('https://steamcommunity.com/my/tradeoffers/privacy') as resp:
                if resp.status == 403 or resp.status == 401:
                    return None

                text = await resp.text()

                # Проверяем на редирект на логин
                if 'login' in text.lower() and 'password' in text.lower():
                    return None

                tree = html.fromstring(text)
                element = tree.xpath('//*[@id="trade_offer_access_url"]')
                if element:
                    return element[0].get('value')
                return None
        except Exception as e:
            logger.error(f"Ошибка получения трейд-ссылки: {e}")
            return None
