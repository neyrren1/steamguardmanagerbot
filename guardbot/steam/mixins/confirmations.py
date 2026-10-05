"""Steam mobile confirmation operations."""

import hashlib
import hmac
import json
import struct
import time
from base64 import b64decode, b64encode

from guardbot.config import logger
from guardbot.steam.constants import USER_AGENT_MOBILE


class ConfirmationsMixin:
    def _create_confirmation_params(self, tag_string: str):
        """Создание параметров для подтверждений"""
        timestamp = int(time.time())
        confirmation_key = self._generate_confirmation_key(self.identity_secret, tag_string, timestamp)

        # 🔥 ИСПРАВЛЕНО: steamid должен быть строкой для параметра 'a'
        steamid_str = str(self.steamid) if self.steamid else ""

        return {
            'p': self.device_id,
            'a': steamid_str,  # 🔥 Убеждаемся что это строка
            'k': confirmation_key.decode() if isinstance(confirmation_key, bytes) else confirmation_key,
            't': timestamp,
            'm': 'android',
            'tag': tag_string
        }

    def _generate_confirmation_key(self, identity_secret: str, tag: str, timestamp: int):
        """Генерация ключа подтверждения"""
        if not identity_secret:
            return None
        buffer = struct.pack('>Q', timestamp) + tag.encode('ascii')
        return b64encode(hmac.new(b64decode(identity_secret), buffer, digestmod=hashlib.sha1).digest())

    async def get_trade_confirmations(self):
        """Получение списка ожидающих подтверждений"""
        logger.info(f"📋 [{self.account_name}] Getting confirmations...")
        logger.info(f"   steamid={self.steamid}")
        logger.info(f"   has_identity_secret={bool(self.identity_secret)}")
        logger.info(f"   has_device_id={bool(self.device_id)}")

        if not self.identity_secret:
            logger.error(f"❌ [{self.account_name}] No identity_secret for confirmations")
            return {'success': False, 'message': 'Нет identity_secret'}

        if not self.steamid:
            logger.error(f"❌ [{self.account_name}] No steamid for confirmations")
            return {'success': False, 'message': 'Нет steamid'}

        if not self.device_id:
            logger.error(f"❌ [{self.account_name}] No device_id")
            return {'success': False, 'message': 'Нет device_id'}

        params = self._create_confirmation_params('conf')
        logger.info(f"📋 [{self.account_name}] Confirmation params: p={params['p'][:20]}..., a={params['a']}, t={params['t']}, tag={params['tag']}")

        headers = {
            'X-Requested-With': 'com.valvesoftware.android.steam.community',
            'User-Agent': USER_AGENT_MOBILE,
        }

        await self._ensure_session()

        # 🔥 Проверяем куки перед запросом
        logger.info(f"🍪 [{self.account_name}] Cookies before confirmation request:")
        for cookie in self.cookie_jar:
            if 'steamcommunity.com' in cookie.get('domain', ''):
                logger.info(f"   cookie present: {cookie.key}")

        async def do_request():
            async with self.session.get(
                'https://steamcommunity.com/mobileconf/getlist',
                params=params,
                headers=headers
            ) as resp:
                text = await resp.text()

                # 🔥 РАСШИРЕННОЕ ЛОГИРОВАНИЕ
                logger.info(f"{'='*60}")
                logger.info(f"📋 [{self.account_name}] STEAM CONFIRMATIONS RESPONSE:")
                logger.info(f"   Status: {resp.status}")
                logger.info(f"   Content-Type: {resp.headers.get('Content-Type', 'unknown')}")
                logger.info(f"   Response length: {len(text)} chars")

                # Проверяем хедеры на needauth
                logger.info(f"   X-EREASON header: {resp.headers.get('X-EREASON', 'none')}")

                logger.info(f"{'='*60}")

                try:
                    data = json.loads(text)

                    # 🔥 ДЕТАЛЬНЫЙ РАЗБОР ОТВЕТА
                    logger.info(f"   Parsed JSON keys: {list(data.keys())}")
                    logger.info(f"   success: {data.get('success')}")
                    logger.info(f"   needauth: {data.get('needauth')}")

                    if data.get('success') and data.get('conf'):
                        logger.info(f"   ✅ Got {len(data['conf'])} confirmations")
                    elif data.get('needauth'):
                        logger.warning(f"   🔒 NEEDAUTH! Session expired or invalid")
                        logger.warning(f"   Message: {data.get('message', 'no message')}")
                    elif not data.get('success'):
                        logger.warning(f"   ❌ success=False, message={data.get('message', 'no message')}")

                    return data
                except json.JSONDecodeError as e:
                    logger.error(f"   ❌ JSON parse error: {e}")
                    return {'success': False, 'message': f'JSON error: {str(e)[:100]}'}

        try:
            data = await do_request()

            if data.get('needauth'):
                logger.warning(f"🔄 [{self.account_name}] Got needauth, trying session recovery...")
                self._recovery_attempted = True

                if await self._try_recover_session():
                    logger.info(f"✅ [{self.account_name}] Session recovered, retrying...")
                    self._session_recovered = True
                    data = await do_request()

                    if data.get('needauth'):
                        logger.error(f"❌ [{self.account_name}] Still needauth after recovery!")
                        self._session_recovery_failed = True
                        return {
                            'success': False,
                            'message': 'Требуется вход в аккаунт',
                            'needauth': True
                        }
                    return data
                else:
                    logger.error(f"❌ [{self.account_name}] Session recovery failed!")
                    self._session_recovery_failed = True
                    return {
                        'success': False,
                        'message': 'Требуется вход в аккаунт',
                        'needauth': True
                    }

            return data

        except Exception as e:
            logger.error(f"❌ [{self.account_name}] Error getting confirmations: {e}", exc_info=True)
            return {'success': False, 'message': str(e)}

    async def send_confirmation(self, conf_id: str, conf_key: str, allow: bool = True) -> bool:
        """Подтверждение или отмена трейда"""
        if not self.identity_secret:
            return False

        # Проверяем sessionid
        try:
            await self._ensure_sessionid()
        except:
            return False

        params = self._create_confirmation_params('allow' if allow else 'cancel')
        params['op'] = 'allow' if allow else 'cancel'
        params['cid'] = conf_id
        params['ck'] = conf_key

        headers = {
            'X-Requested-With': 'XMLHttpRequest',
            'User-Agent': USER_AGENT_MOBILE,
        }

        await self._ensure_session()
        try:
            async with self.session.get(
                'https://steamcommunity.com/mobileconf/ajaxop',
                params=params,
                headers=headers
            ) as resp:
                text = await resp.text()

                try:
                    data = await resp.json()
                except:
                    import json
                    data = json.loads(text)

                # Проверяем на needauth
                if data.get('needauth'):
                    logger.warning("Подтверждение требует аутентификации")
                    return False

                return data.get('success', False)
        except Exception as e:
            logger.error(f"Ошибка отправки подтверждения: {e}")
            return False

    async def confirm_market_listing(self, conf_id: str, allow: bool = True) -> bool:
        """Специальный метод для подтверждения/отмены продаж на маркете (type=3)"""
        if not self.identity_secret:
            logger.error("No identity_secret for market listing confirmation")
            return False

        try:
            await self._ensure_sessionid()
        except Exception as e:
            logger.error(f"Failed to get sessionid: {e}")
            return False

        # Получаем sessionid из cookies
        sessionid = None
        for cookie in self.cookie_jar:
            if cookie.key == 'sessionid' and 'steamcommunity.com' in cookie.get('domain', ''):
                sessionid = cookie.value
                break

        if not sessionid:
            logger.error("No sessionid found")
            return False

        timestamp = int(time.time())

        # Генерируем confirmation key
        buffer = struct.pack('>Q', timestamp) + (b'allow' if allow else b'cancel')
        confirmation_key = b64encode(hmac.new(b64decode(self.identity_secret), buffer, digestmod=hashlib.sha1).digest())

        # Формируем данные для POST запроса
        data = {
            'p': self.device_id,
            'a': str(self.steamid),
            'k': confirmation_key.decode(),
            't': timestamp,
            'm': 'android',
            'tag': 'allow' if allow else 'cancel',
            'cid': conf_id,
            'sessionid': sessionid
        }

        headers = {
            'X-Requested-With': 'XMLHttpRequest',
            'User-Agent': USER_AGENT_MOBILE,
            'Content-Type': 'application/x-www-form-urlencoded',
            'Referer': 'https://steamcommunity.com/mobileconf/conf'
        }

        await self._ensure_session()

        try:
            logger.info(f"📤 Confirming market listing {conf_id}, allow={allow}")

            async with self.session.post(
                'https://steamcommunity.com/mobileconf/ajaxop',
                data=data,
                headers=headers
            ) as resp:
                response_text = await resp.text()
                logger.info("📥 Market confirmation response received (%s bytes)", len(response_text))

                try:
                    result = json.loads(response_text)
                except:
                    result = {'success': False}

                if result.get('success'):
                    logger.info(f"✅ Market listing {conf_id} confirmed/cancelled successfully")
                    return True
                else:
                    logger.error(f"❌ Failed: {result}")
                    return False

        except Exception as e:
            logger.error(f"Error confirming market listing: {e}", exc_info=True)
            return False
