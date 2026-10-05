"""Steam Community Market operations."""

import json

from guardbot.config import logger
from guardbot.steam.constants import USER_AGENT_MOBILE


class MarketMixin:
    async def sell_item(self, app_id: int, context_id: int, assetid: str, amount: int, price: int) -> dict:
        """Продажа предмета на Steam Market"""

        logger.info(f"💰 [{self.account_name}] SELL ITEM START:")
        logger.info(f"   app_id={app_id}, context_id={context_id}, assetid={assetid}, amount={amount}, price={price}")

        # 🔥 Проверяем куки ДО продажи
        logger.info(f"🍪 [{self.account_name}] Cookies before sell:")
        for cookie in self.cookie_jar:
            if 'steamcommunity.com' in cookie.get('domain', ''):
                logger.info(f"   cookie present: {cookie.key}")

        # Гарантируем наличие sessionid
        try:
            await self._ensure_sessionid()
            logger.info(f"🔑 [{self.account_name}] sessionid is available before sell")
        except Exception as e:
            logger.error(f"❌ [{self.account_name}] Failed to get sessionid for sell: {e}")
            return {
                'success': False,
                'error': 'Требуется вход в аккаунт',
                'needauth': True
            }

        sessionid = None
        for cookie in self.cookie_jar:
            if cookie.key == 'sessionid' and 'steamcommunity.com' in cookie.get('domain', ''):
                sessionid = cookie.value
                break

        if not sessionid:
            logger.error(f"❌ [{self.account_name}] No sessionid in cookies!")
            return {
                'success': False,
                'error': 'Требуется вход в аккаунт',
                'needauth': True
            }

        payload = {
            'sessionid': sessionid,
            'appid': str(app_id),
            'contextid': str(context_id),
            'assetid': str(assetid),
            'amount': str(amount),
            'price': str(price)
        }

        headers = {
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'Origin': 'https://steamcommunity.com',
            'Referer': f'https://steamcommunity.com/profiles/{self.steamid}/inventory/',
            'User-Agent': USER_AGENT_MOBILE,
            'X-Requested-With': 'XMLHttpRequest',
        }

        await self._ensure_session()

        try:
            logger.info(f"📤 [{self.account_name}] Sending sell request to Steam...")

            async with self.session.post(
                'https://steamcommunity.com/market/sellitem/',
                data=payload,
                headers=headers
            ) as resp:
                response_text = await resp.text()

                # 🔥 РАСШИРЕННОЕ ЛОГИРОВАНИЕ
                logger.info(f"{'='*60}")
                logger.info(f"💰 [{self.account_name}] SELL ITEM RESPONSE:")
                logger.info(f"   HTTP Status: {resp.status}")
                logger.info(f"   Content-Type: {resp.headers.get('Content-Type', 'unknown')}")
                logger.info(f"   X-EREASON: {resp.headers.get('X-EREASON', 'none')}")
                logger.info(f"   Response length: {len(response_text)} chars")
                logger.info(f"{'='*60}")

                # Проверка на needauth
                if resp.status in [401, 403]:
                    logger.error(f"❌ [{self.account_name}] Sell HTTP {resp.status} - NEED AUTH!")
                    return {
                        'success': False,
                        'error': f'HTTP {resp.status}: Требуется вход в аккаунт',
                        'needauth': True
                    }

                if 'login' in response_text.lower() and 'steam' in response_text.lower():
                    logger.error(f"❌ [{self.account_name}] Sell response contains login page - NEED AUTH!")
                    return {
                        'success': False,
                        'error': 'Требуется вход в аккаунт',
                        'needauth': True
                    }

                try:
                    result = json.loads(response_text)
                    logger.info(f"   Parsed result keys: {list(result.keys())}")
                    logger.info(f"   success: {result.get('success')}")
                    logger.info(f"   Entire result: {json.dumps(result, ensure_ascii=False)[:500]}")

                    if result.get('success'):
                        logger.info(f"✅ [{self.account_name}] Sell SUCCESS!")
                        return {
                            'success': True,
                            'requires_confirmation': result.get('requires_confirmation', 0),
                            'needs_mobile_confirmation': result.get('needs_mobile_confirmation', False),
                            'needs_email_confirmation': result.get('needs_email_confirmation', False),
                            'email_domain': result.get('email_domain', '')
                        }
                    else:
                        error_msg = result.get('message', result.get('strError', 'Неизвестная ошибка'))
                        logger.error(f"❌ [{self.account_name}] Sell FAILED: {error_msg}")
                        return {'success': False, 'error': error_msg}

                except json.JSONDecodeError as e:
                    logger.error(f"❌ [{self.account_name}] Sell returned non-JSON: {e}")
                    logger.error("   Sell response body omitted from logs")
                    return {
                        'success': False,
                        'error': 'Steam вернул некорректный ответ'
                    }

        except Exception as e:
            logger.error(f"❌ [{self.account_name}] Error selling item: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

    async def sell_item_with_recovery(self, app_id: int, context_id: int, assetid: str, amount: int, price: int) -> dict:
        """Продажа предмета с автоматическим восстановлением сессии при needauth"""
        result = await self.sell_item(app_id, context_id, assetid, amount, price)

        if not result.get('success'):
            error = result.get('error', '')
            # Проверяем признаки протухшей сессии
            if any(keyword in error.lower() for keyword in ['refresh the page', 'try again', 'login', 'unauthorized', 'auth']):
                logger.warning(f"🔄 [{self.account_name}] Sell failed with '{error}', trying session recovery...")
                self._recovery_attempted = True

                if await self._try_recover_session():
                    logger.info(f"✅ [{self.account_name}] Session recovered, retrying sell...")
                    self._session_recovered = True
                    result = await self.sell_item(app_id, context_id, assetid, amount, price)
                else:
                    logger.error(f"❌ [{self.account_name}] Session recovery failed!")
                    self._session_recovery_failed = True
                    return {
                        'success': False,
                        'error': 'Требуется вход в аккаунт',
                        'needauth': True
                    }

        return result
