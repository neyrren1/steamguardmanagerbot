"""Extracted from the legacy bot module without behavior changes."""

from PIL import Image, ImageDraw, ImageFont
from aiohttp_socks import ProxyConnector
from base64 import b64decode, b64encode
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding, rsa as crypto_rsa
from datetime import datetime, timezone
from guardbot.config import logger
from lxml import html
from sqlalchemy import text
from typing import Dict, List, Optional
from yarl import URL
import aiohttp
import base64
import hashlib
import hmac
import io
import json
import re
import struct
import time
import uuid

# ============================================================
# STEAM MOBILE CLIENT - ИСКЛЮЧЕНИЯ
# ============================================================

class UnknownException(Exception): pass
class RefreshTokenEmpty(Exception): pass
class RefreshTokenExpired(Exception): pass
class InvalidCredentials(Exception): pass
class InvalidSteamGuardCode(Exception): pass
class NoAccountName(Exception): pass
class AlreadyHasAPhoneNumber(Exception): pass
class EmailNotVerified(Exception): pass
class InvalidSMSCode(Exception): pass
class AlreadyHasMobileSteamGuard(Exception): pass
class UnableToGenerateCorrectCodes(Exception): pass

# ============================================================
# STEAM MOBILE CLIENT - СЕССИЯ (ИСПРАВЛЕННАЯ)
# ============================================================

USER_AGENT_MOBILE = 'Dalvik/2.1.0 (Linux; U; Android 9; Valve Steam App Version/3)'

def parse_proxy_string(proxy_str: str) -> Optional[str]:
    """
    Проверяет и исправляет формат прокси
    Поддерживает: http://, https://, socks4://, socks5://
    """
    if not proxy_str:
        return None

    # Уже есть протокол
    if proxy_str.startswith(('http://', 'https://', 'socks4://', 'socks5://')):
        return proxy_str

    # Определяем тип прокси по порту (эвристика)
    if ':1080' in proxy_str or ':1081' in proxy_str or ':46281' in proxy_str:
        proxy_str = 'socks5://' + proxy_str
        logger.info(f"Auto-detected SOCKS5 proxy: {proxy_str}")
    elif ':3128' in proxy_str or ':8080' in proxy_str or ':8443' in proxy_str:
        proxy_str = 'http://' + proxy_str
        logger.info(f"Auto-detected HTTP proxy: {proxy_str}")
    else:
        # По умолчанию HTTP
        proxy_str = 'http://' + proxy_str
        logger.info(f"Defaulting to HTTP proxy: {proxy_str}")

    return proxy_str

# ============================================================
# STEAM MOBILE CLIENT - СЕССИЯ (ПОЛНАЯ ВЕРСИЯ)
# ============================================================


class AsyncSteamSession:
    def __init__(self, account_name: str, password: str, proxy: Optional[str] = None) -> None:
        self.account_name = account_name
        self._password = password
        self.proxy = parse_proxy_string(proxy)
        self.steamid = None
        self.access_token = None
        self.refresh_token = None
        self.session_id = None
        self.__steamTimeDiff = 0
        self.__steamTimeAligned = False
        self.__steamTimeSyncedWithProxy = False
        self.session: Optional[aiohttp.ClientSession] = None
        self.cookie_jar = aiohttp.CookieJar()

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def _ensure_session(self):
        # 🔥 ВСЕГДА пересоздаём сессию, если она без прокси или прокси изменился
        if self.session and not self.session.closed:
            is_proxy_connector = 'ProxyConnector' in str(type(self.session.connector))

            # Если нет прокси в коннекторе, но self.proxy задан — пересоздаём
            # Если есть прокси в коннекторе, но self.proxy не задан — пересоздаём
            if (self.proxy and not is_proxy_connector) or (not self.proxy and is_proxy_connector):
                logger.info(f"🔄 Прокси изменился, пересоздаю сессию...")
                await self.session.close()
                self.session = None

        if self.session is None or self.session.closed:
            headers = {
                'User-Agent': USER_AGENT_MOBILE,
                'Accept': 'application/json, text/plain, */*',
                'Accept-Language': 'en-US,en;q=0.9',
                'Connection': 'keep-alive',
            }

            timeout = aiohttp.ClientTimeout(
                total=60,
                connect=30,
                sock_read=30,
                sock_connect=30
            )

            # 🔥 ПРИНУДИТЕЛЬНО используем прокси
            if self.proxy:
                logger.info(f"🔧 Создаю ProxyConnector для: {self.proxy}")
                connector = ProxyConnector.from_url(self.proxy, ssl=False)
            else:
                # 🔥 Если прокси нет — ПАДАЕМ С ОШИБКОЙ
                raise Exception("❌ ПРОКСИ НЕ НАСТРОЕН! Запросы без прокси запрещены.")

            self.session = aiohttp.ClientSession(
                headers=headers,
                cookie_jar=self.cookie_jar,
                connector=connector,
                timeout=timeout
            )
            logger.info(f"✅ Сессия создана с ProxyConnector")

    async def close(self):
        """Закрытие сессии"""
        if self.session and not self.session.closed:
            await self.session.close()

    async def _request(self, method: str, url: str, **kwargs):
        """
        Выполняет HTTP запрос с поддержкой прокси
        """

        if not self.proxy:
            raise Exception("❌ ЗАПРОС БЕЗ ПРОКСИ ЗАПРЕЩЁН!")

        await self._ensure_session()

        # Убеждаемся что сессия использует ProxyConnector
        if 'ProxyConnector' not in str(type(self.session.connector)):
            raise Exception("❌ Сессия использует обычный коннектор вместо прокси!")

        # Логируем прокси
        logger.info(f"📡 Запрос: {method} {url}, Прокси: {self.proxy}")

        # Правильно добавляем прокси
        # if self.proxy:
        #     kwargs['proxy'] = self.proxy

        # Добавляем заголовки
        if 'headers' not in kwargs:
            kwargs['headers'] = {}

        kwargs['headers'].update({
            'User-Agent': USER_AGENT_MOBILE,
            'Accept': 'application/json, text/plain, */*',
        })

        # Отключаем SSL проверку
        kwargs['ssl'] = False

        try:
            async with self.session.request(method, url, **kwargs) as resp:
                await resp.read()
                logger.info(f"✅ Ответ: {resp.status} для {url}")
                return resp
        except Exception as e:
            logger.error(f"❌ Ошибка запроса {url}: {e}")
            raise

    async def refresh_access_token(self):
        """Обновление access token через refresh token"""
        if not self.refresh_token:
            raise RefreshTokenEmpty('Refresh token is empty')
        if self.is_token_expired(self.refresh_token):
            raise RefreshTokenExpired('Refresh token expired')

        data = {'steamid': self.steamid, 'refresh_token': self.refresh_token}

        resp = await self._request(
            'POST',
            "https://api.steampowered.com/IAuthenticationService/GenerateAccessTokenForApp/v1",
            data=data
        )
        async with resp:
            res = await resp.json()
            res = res.get('response')
            self.access_token = res.get('access_token', self.access_token)
            self.refresh_token = res.get('refresh_token', self.refresh_token)
            pre = str(self.steamid) + '%7C%7C'

            self.cookie_jar.update_cookies(
                {'steamLoginSecure': pre + self.access_token},
                URL('https://steamcommunity.com')
            )
            self.cookie_jar.update_cookies(
                {'steamLoginSecure': pre + self.access_token},
                URL('https://store.steampowered.com')
            )
            self.cookie_jar.update_cookies(
                {'steamRefresh_steam': pre + self.refresh_token},
                URL('https://login.steampowered.com')
            )

        # 🔥 ВАЖНО: После обновления токена нужно получить sessionid
        await self._ensure_sessionid()

    async def _ensure_sessionid(self):
        """Гарантирует наличие sessionid в cookies"""
        # Проверяем, есть ли уже sessionid
        for cookie in self.cookie_jar:
            if cookie.key == 'sessionid' and 'steamcommunity.com' in cookie.get('domain', ''):
                logger.info(f"✅ [{self.account_name}] sessionid already exists: {cookie.value[:20]}... (expires: {cookie.get('expires', 'session')})")
                self.session_id = cookie.value
                return cookie.value

        # Если нет - получаем через ОДИН запрос к главной странице
        logger.info(f"🔄 [{self.account_name}] sessionid not found, fetching from Steam Community...")

        headers = {
            'User-Agent': USER_AGENT_MOBILE,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        }

        try:
            async with self.session.get('https://steamcommunity.com/', headers=headers, ssl=False) as resp:
                logger.info(f"📡 [{self.account_name}] Steam Community response: HTTP {resp.status}")

                text_preview = (await resp.text())[:300]
                logger.info(f"📄 [{self.account_name}] Response preview: {text_preview}")

                # Логируем ВСЕ куки которые пришли
                logger.info(f"🍪 [{self.account_name}] Cookies after request:")
                new_sessionid = None
                for cookie in self.cookie_jar:
                    logger.info(f"   {cookie.key}: {cookie.value[:30]}... (domain: {cookie.get('domain', '')}, expires: {cookie.get('expires', 'session')})")
                    if cookie.key == 'sessionid' and 'steamcommunity.com' in cookie.get('domain', ''):
                        new_sessionid = cookie.value

                if new_sessionid:
                    logger.info(f"✅ [{self.account_name}] Got NEW sessionid: {new_sessionid[:20]}...")
                    self.session_id = new_sessionid
                    return new_sessionid
                else:
                    logger.error(f"❌ [{self.account_name}] sessionid NOT found in response cookies!")
                    # 🔥 Проверяем — может редирект на логин?
                    if 'login' in text_preview.lower() or 'steam_openid' not in text_preview:
                        logger.error(f"🔒 [{self.account_name}] Looks like we got login page instead of community!")

        except Exception as e:
            logger.error(f"❌ [{self.account_name}] Failed to get sessionid: {e}", exc_info=True)
            raise Exception(f"Failed to obtain sessionid: {e}")

    def is_token_expired(self, token: str) -> bool:
        """Проверка истек ли токен"""
        if not token:
            return True
        try:
            return datetime.now(timezone.utc).timestamp() > self.get_token_expire_timestamp(token)
        except:
            return True

    def get_token_expire_timestamp(self, token: str) -> int:
        """Получение timestamp истечения токена"""
        if not token:
            return 0
        try:
            token_components = token.split('.')
            base64_str = token_components[1].replace('-', '+').replace('_', '/')
            if len(base64_str) % 4 != 0:
                base64_str += '=' * (4 - len(base64_str) % 4)
            payload_bytes = base64.b64decode(base64_str)
            return json.loads(payload_bytes.decode('utf-8')).get('exp', 0)
        except:
            return 0

    def get_steam_time(self) -> int:
        """Получение синхронизированного Steam времени"""
        if not self.__steamTimeAligned:
            # Если время не синхронизировано, используем локальное
            logger.warning("Steam time not aligned, using local time")
            return int(time.time())
        return int(time.time() + self.__steamTimeDiff)

    async def align_time(self):
        """Синхронизация времени со Steam серверами"""
        currentTime = int(time.time())
        params = {'steamid': '0'}

        logger.info(f"🕐 Начинаю синхронизацию времени. Прокси: {bool(self.proxy)}")

        try:
            resp = await self._request(
                'POST',
                'https://api.steampowered.com/ITwoFactorService/QueryTime/v0001',
                params=params
            )
            async with resp:
                res = await resp.json()
                logger.info(f"📡 Ответ от Steam Time API: {res}")

                res = res.get('response')
                server_time = int(res.get('server_time'))
                self.__steamTimeAligned = True
                self.__steamTimeDiff = server_time - currentTime
                self.__steamTimeSyncedWithProxy = bool(self.proxy)
                logger.info(f"✅ Steam время синхронизировано. Сервер: {server_time}, Локальное: {currentTime}, Разница: {self.__steamTimeDiff}")
        except Exception as e:
            logger.error(f"❌ Ошибка синхронизации времени: {e}", exc_info=True)

            if not self.proxy:
                logger.info("Прокси нет, использую локальное время")
                self.__steamTimeAligned = True
                self.__steamTimeDiff = 0
                self.__steamTimeSyncedWithProxy = False
            else:
                logger.warning(f"Прокси есть но синхронизация не удалась, использую локальное время")
                self.__steamTimeAligned = True
                self.__steamTimeDiff = 0
                self.__steamTimeSyncedWithProxy = False

    # Вместо прямого доступа к __steamTimeDiff, добавим метод:
    def is_using_steam_time(self) -> bool:
        """Проверяет, используется ли синхронизированное Steam время"""
        return self.__steamTimeAligned and self.__steamTimeDiff != 0 and self.__steamTimeSyncedWithProxy

    def export(self) -> dict:
        """Экспорт всех данных сессии"""
        return {
            'steamid': self.steamid,
            'access_token': self.access_token,
            'refresh_token': self.refresh_token,
            'session_id': self.session_id,
            'account_name': self.account_name,
            'cookies': self.export_cookies_dict()
        }

    def export_cookies_dict(self) -> List[Dict]:
        """Экспорт куки в словарь"""
        cookies = []
        for cookie in self.cookie_jar:
            cookie_dict = {
                'name': cookie.key,
                'value': cookie.value,
                'domain': cookie.get('domain', ''),
                'path': cookie.get('path', '/'),
                'secure': cookie.get('secure', False),
                'expires': cookie.get('expires', None)
            }
            cookies.append(cookie_dict)
        return cookies

    async def load(self, data: dict):
        """Загрузка данных сессии"""
        if not data:
            return
        self.steamid = data.get('steamid')
        self.access_token = data.get('access_token')
        self.refresh_token = data.get('refresh_token')
        self.session_id = data.get('session_id')

        # Загружаем куки
        cookies_data = data.get('cookies', [])
        if cookies_data:
            await self.load_cookies_from_dict(cookies_data)

    async def load_cookies_from_dict(self, cookies_list: List[Dict]):
        """Загрузка куки из словаря"""
        if not cookies_list:
            return

        for cookie_dict in cookies_list:
            try:
                self.cookie_jar.update_cookies(
                    {cookie_dict['name']: cookie_dict['value']},
                    URL(f"https://{cookie_dict['domain']}")
                )
            except Exception as e:
                logger.warning(f"Failed to load cookie {cookie_dict.get('name')}: {e}")

# ============================================================
# STEAM MOBILE CLIENT - API ФУНКЦИИ (ПОЛНАЯ ЗАМЕНА)
# ============================================================

def generate_device_id():
    return 'android:' + str(uuid.uuid4())

async def addAuthenticator(session: AsyncSteamSession, access_token: str, steamid: str,
                          device_id: str, authenticator_time: str = None):
    data = {
        'steamid': steamid,
        'authenticator_type': '1',
        'device_identifier': device_id,
        'sms_phone_id': '1',
        'version': 2
    }
    params = {'access_token': access_token}
    url = 'https://api.steampowered.com/ITwoFactorService/AddAuthenticator/v1'

    resp = await session._request('POST', url, params=params, data=data)
    async with resp:
        if resp.status != 200:
            return None
        res = await resp.json()
        return res.get('response')

async def finalizeAddAuthenticator(session: AsyncSteamSession, access_token: str, steamid: str,
                                  code: str, steam_guard_code: str, authenticator_time: str):
    data = {
        'steamid': steamid,
        'activation_code': code,
        'authenticator_code': steam_guard_code,
        'authenticator_time': authenticator_time,
    }
    params = {'access_token': access_token}
    url = 'https://api.steampowered.com/ITwoFactorService/FinalizeAddAuthenticator/v1'

    resp = await session._request('POST', url, params=params, data=data)
    async with resp:
        res = await resp.json()
        return res.get('response')

async def getPasswordRSAPublicKey(session: AsyncSteamSession, account_name: str):
    """Получение RSA публичного ключа для шифрования пароля"""
    url = 'https://api.steampowered.com/IAuthenticationService/GetPasswordRSAPublicKey/v1'

    resp = await session._request('GET', url, params={'account_name': account_name})
    async with resp:
        res = await resp.json()
        res = res.get('response')

        # Создаём публичный ключ через cryptography
        publickey_mod = int(res['publickey_mod'], 16)
        publickey_exp = int(res['publickey_exp'], 16)

        public_key_numbers = crypto_rsa.RSAPublicNumbers(publickey_exp, publickey_mod)
        public_key = public_key_numbers.public_key()

        return public_key, int(res['timestamp'])

async def beginAuthSessionViaCredentials(session: AsyncSteamSession, account_name: str,
                                        encrypted_password: str, encryption_timestamp: int):
    """Начало авторизационной сессии"""
    params = {
        'account_name': account_name,
        'encrypted_password': encrypted_password,
        'encryption_timestamp': encryption_timestamp,
        'persistence': '1',
        'remember_login': 'true',
        'platform_type': '3',
    }
    url = 'https://api.steampowered.com/IAuthenticationService/BeginAuthSessionViaCredentials/v1'

    resp = await session._request('POST', url, params=params)
    async with resp:
        res = await resp.json()
        return res.get('response')

async def updateAuthSessionWithSteamGuardCode(session: AsyncSteamSession, client_id: str,
                                              steamid: str, code: str, code_type: str = '2'):
    """Обновление сессии с кодом Steam Guard"""
    params = {
        'client_id': client_id,
        'steamid': steamid,
        'code': code,
        'code_type': code_type
    }
    url = 'https://api.steampowered.com/IAuthenticationService/UpdateAuthSessionWithSteamGuardCode/v1'

    resp = await session._request('POST', url, params=params)
    async with resp:
        return resp

async def pollAuthSessionStatus(session: AsyncSteamSession, client_id: str, request_id: str) -> dict:
    """Проверка статуса авторизационной сессии"""
    params = {
        'client_id': client_id,
        'request_id': request_id
    }
    url = 'https://api.steampowered.com/IAuthenticationService/PollAuthSessionStatus/v1'

    resp = await session._request('POST', url, params=params)
    async with resp:
        res = await resp.json()
        return res.get('response')

async def finalizelogin(session: AsyncSteamSession, refresh_token: str, steamid: str = None):
    """Завершение процесса входа"""
    params = {
        'nonce': refresh_token,
        'sessionid': '',
        'redir': 'https://steamcommunity.com/login/home/?goto='
    }
    url = 'https://login.steampowered.com/jwt/finalizelogin'

    resp = await session._request('POST', url, data=params)
    async with resp:
        try:
            res = await resp.json()
        except:
            text = await resp.text()
            logger.error(f"finalizelogin non-JSON response: {text[:500]}")
            return None

        if not res.get('success', True):
            logger.error(f"finalizelogin failed: {res}")
            return None

        steamID = res.get('steamID')
        transfer_info = res.get('transfer_info', [])
        for transfer in transfer_info:
            transfer_url = transfer.get('url')
            if transfer_url not in ['https://store.steampowered.com/login/settoken',
                                   'https://steamcommunity.com/login/settoken']:
                continue
            transfer_params = transfer.get('params')
            transfer_params['steamID'] = steamID

            transfer_resp = await session._request('POST', transfer_url, params=transfer_params)
            async with transfer_resp:
                pass

        # 🔥 Получаем sessionid после установки кук
        await session._ensure_sessionid()

        return res


# ============================================================
# STEAM MOBILE CLIENT - ОСНОВНОЙ КЛАСС
# ============================================================

class LoginConfirmType:
    none = 1
    email = 2
    mobile = 3

class AsyncSteamMobile(AsyncSteamSession):
    def __init__(self, account_name: str, password: str, proxy: Optional[str] = None) -> None:
        self.__client_id = None
        self.__request_id = None
        self.__code_type = None
        self.device_id = generate_device_id()
        self.shared_secret = None
        self.serial_number = None
        self.revocation_code = None
        self.uri = None
        self.token_gid = None
        self.identity_secret = None
        self.secret_1 = None
        self._session_recovered = False
        self._session_recovery_failed = False
        self._recovery_attempted = False
        super().__init__(account_name=account_name, password=password, proxy=proxy)

    def load_mobile(self, data: dict):
        if not data:
            return
        self.device_id = data.get('device_id')
        self.shared_secret = data.get('shared_secret')
        self.serial_number = data.get('serial_number')
        self.revocation_code = data.get('revocation_code')
        self.uri = data.get('uri')
        self.token_gid = data.get('token_gid')
        self.identity_secret = data.get('identity_secret')
        self.secret_1 = data.get('secret_1')

    async def login(self) -> int:
        await self._ensure_session()
        await self.align_time()
        key, encryption_timestamp = await getPasswordRSAPublicKey(self, self.account_name)
        encrypted_pass = b64encode(key.encrypt(
            self._password.encode('utf-8'),
            asym_padding.PKCS1v15()
        )).decode()
        res = await beginAuthSessionViaCredentials(
            self, self.account_name, encrypted_pass, encryption_timestamp
        )
        if not res.get('steamid') or not res.get('client_id') or not res.get('request_id'):
            raise InvalidCredentials()
        self.steamid = str(res.get('steamid'))  # Конвертируем в строку
        self.__client_id = res.get('client_id')
        self.__request_id = res.get('request_id')
        allowed_confirmations = res.get('allowed_confirmations')
        self.__code_type = int(allowed_confirmations[0].get('confirmation_type'))
        return self.__code_type

    async def confirm_login(self, steam_guard_code: str = None):
        await self._ensure_session()
        if self.__code_type != 1:
            await updateAuthSessionWithSteamGuardCode(
                self, self.__client_id, self.steamid,
                steam_guard_code, str(self.__code_type)
            )
        res = await pollAuthSessionStatus(self, self.__client_id, self.__request_id)
        if not res.get('account_name') or not res.get('access_token') or not res.get('refresh_token'):
            raise InvalidSteamGuardCode()

        # Сохраняем steamid как строку
        if res.get('steamid'):
            self.steamid = str(res.get('steamid'))

        self.access_token = res.get('access_token')
        self.refresh_token = res.get('refresh_token')
        await finalizelogin(self, self.refresh_token)

        # 🔥 sessionid уже получен внутри finalizelogin

    def generate_steam_guard_code(self):
        """Генерация Steam Guard кода"""
        try:
            # Пробуем получить синхронизированное время
            steam_time = self.get_steam_time()
        except Exception as e:
            # Если не удалось, используем локальное время
            logger.warning(f"Failed to get Steam time: {e}, using local time")
            steam_time = int(time.time())

        return self.generate_steam_guard_code_for_time(steam_time)

    def generate_steam_guard_code_for_time(self, t: int):
        if not self.shared_secret:
            return None
        time_buffer = struct.pack('>Q', t // 30)
        time_hmac = hmac.new(base64.b64decode(self.shared_secret), time_buffer, digestmod=hashlib.sha1).digest()
        begin = time_hmac[19] & 0xF
        full_code = struct.unpack('>I', time_hmac[begin:begin + 4])[0] & 0x7FFFFFFF
        chars = '23456789BCDFGHJKMNPQRTVWXY'
        code = ''
        for _ in range(5):
            full_code, i = divmod(full_code, len(chars))
            code += chars[i]
        return code

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

            async with self.session.get(url, headers=headers, ssl=False, allow_redirects=True) as resp:
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
                    logger.info(f"📋 data-config preview: {config_match.group(1)[:300]}")
                else:
                    # 🔥 Логируем первые 500 символов HTML чтобы понять что пришло
                    logger.warning(f"🌍 No data-config found. HTML preview: {html_text[:500]}")

        except Exception as e:
            logger.error(f"Failed to get account country: {e}", exc_info=True)

        return None

    def export(self):
        """Экспорт всех данных сессии включая мобильные"""
        data = super().export()  # Получаем базовые данные из AsyncSteamSession
        # Добавляем мобильные данные
        data.update({
            'device_id': self.device_id,
            'shared_secret': self.shared_secret,
            'serial_number': self.serial_number,
            'revocation_code': self.revocation_code,
            'uri': self.uri,
            'token_gid': self.token_gid,
            'identity_secret': self.identity_secret,
            'secret_1': self.secret_1
        })
        return data

    def export_mobile(self):
        """Экспорт только мобильных данных"""
        return {
            'device_id': self.device_id,
            'shared_secret': self.shared_secret,
            'serial_number': self.serial_number,
            'revocation_code': self.revocation_code,
            'uri': self.uri,
            'token_gid': self.token_gid,
            'identity_secret': self.identity_secret,
            'secret_1': self.secret_1
        }

    async def get_tradelink(self) -> Optional[str]:
        """Получение трейд-ссылки"""
        await self._ensure_session()

        # Проверяем sessionid
        try:
            await self._ensure_sessionid()
        except:
            return None

        try:
            async with self.session.get('https://steamcommunity.com/my/tradeoffers/privacy', ssl=False) as resp:
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

    async def get_inventory(self, app_id: int = 730, context_id: int = 2):
        """Получение инвентаря"""
        url = f"https://steamcommunity.com/inventory/{self.steamid}/{app_id}/{context_id}?l=english&count=2000"


        await self._ensure_session()

        headers = {
            'User-Agent': USER_AGENT_MOBILE,
            'Accept': 'application/json',
            'Referer': f'https://steamcommunity.com/profiles/{self.steamid}/inventory/',
        }

        try:
            async with self.session.get(url, headers=headers, ssl=False) as resp:
                logger.info(f"Inventory status: {resp.status} for {self.account_name} (app {app_id})")
                if resp.status != 200:
                    # 🔥 Логируем тело ошибки
                    try:
                        error_text = await resp.text()
                        logger.error(f"❌ Inventory HTTP {resp.status} for {self.account_name}: {error_text[:500]}")
                    except:
                        logger.error(f"❌ Inventory HTTP {resp.status} for {self.account_name} (no body)")
                    return None

                # 🔥 Проверяем Content-Type
                content_type = resp.headers.get('Content-Type', '')
                if 'json' not in content_type:
                    text = await resp.text()
                    logger.error(f"❌ Inventory returned non-JSON for {self.account_name}: Content-Type={content_type}")
                    logger.error(f"Response preview: {text[:500]}")
                    return None

                data = await resp.json()

                # 🔥 Логируем странные ответы
                if not data or data.get('success') == False:
                    logger.warning(f"⚠️ Empty or failed inventory for {self.account_name}: {json.dumps(data, ensure_ascii=False)[:300]}")
                else:
                    logger.info(f"✅ Inventory loaded for {self.account_name}: {data.get('total_inventory_count', '?')} items")

                return data

        except Exception as e:
            logger.error(f"Inventory error: {e}")
            raise

    async def get_tradeable_inventory(self) -> List[Dict]:
        """Получение предметов доступных для трейда"""
        inventory = await self.get_inventory()

        if not inventory or inventory.get('total_inventory_count', 0) == 0:
            return []

        assets = inventory.get('assets', [])
        descriptions = inventory.get('descriptions', [])

        ready_assets = []
        for asset in assets:
            classid = asset['classid']
            assetid = asset['assetid']

            for desc in descriptions:
                if desc['classid'] == classid and desc.get('tradable'):
                    ready_assets.append({
                        "assetid": assetid,
                        "classid": classid,
                        "name": desc.get('market_name', desc.get('name', 'Unknown')),
                        "tradable": True,
                        'tradeoffer_asset': {"appid": 730, "contextid": "2", "amount": 1, "assetid": assetid}
                    })
                    break
        return ready_assets


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

    async def sell_item(self, app_id: int, context_id: int, assetid: str, amount: int, price: int) -> dict:
        """Продажа предмета на Steam Market"""

        logger.info(f"💰 [{self.account_name}] SELL ITEM START:")
        logger.info(f"   app_id={app_id}, context_id={context_id}, assetid={assetid}, amount={amount}, price={price}")

        # 🔥 Проверяем куки ДО продажи
        logger.info(f"🍪 [{self.account_name}] Cookies before sell:")
        for cookie in self.cookie_jar:
            if 'steamcommunity.com' in cookie.get('domain', ''):
                logger.info(f"   {cookie.key}: {cookie.value[:30]}... (expires: {cookie.get('expires', 'session')})")

        # Гарантируем наличие sessionid
        try:
            sessionid_before = await self._ensure_sessionid()
            logger.info(f"🔑 [{self.account_name}] sessionid before sell: {sessionid_before[:20]}...")
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
                headers=headers,
                ssl=False
            ) as resp:
                response_text = await resp.text()

                # 🔥 РАСШИРЕННОЕ ЛОГИРОВАНИЕ
                logger.info(f"{'='*60}")
                logger.info(f"💰 [{self.account_name}] SELL ITEM RESPONSE:")
                logger.info(f"   HTTP Status: {resp.status}")
                logger.info(f"   Content-Type: {resp.headers.get('Content-Type', 'unknown')}")
                logger.info(f"   X-EREASON: {resp.headers.get('X-EREASON', 'none')}")
                logger.info(f"   Response length: {len(response_text)} chars")
                logger.info(f"   Response (first 500): {response_text[:500]}")

                if len(response_text) < 2000:
                    logger.info(f"   📄 Full response: {response_text}")
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
                    logger.error(f"   Raw: {response_text[:1000]}")
                    return {
                        'success': False,
                        'error': f'Неверный ответ (не JSON): {response_text[:200]}'
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

    async def get_inventory_paginated(self, app_id: int = 730, context_id: int = 2,
                                    start_assetid: str = None, count: int = 15):
        """Получение инвентаря с пагинацией"""
        url = f"https://steamcommunity.com/inventory/{self.steamid}/{app_id}/{context_id}"

        params = {
            'l': 'english',
            'count': count
        }

        if start_assetid and start_assetid != "prev":
            params['start_assetid'] = start_assetid

        await self._ensure_session()

        # Проверяем sessionid
        try:
            await self._ensure_sessionid()
        except:
            pass

        headers = {
            'User-Agent': USER_AGENT_MOBILE,
            'Accept': 'application/json',
            'Referer': f'https://steamcommunity.com/profiles/{self.steamid}/inventory/',
        }

        try:
            async with self.session.get(url, headers=headers, params=params, ssl=False) as resp:
                if resp.status == 403 or resp.status == 401:
                    return {'error': 'Требуется вход в аккаунт', 'needauth': True}

                if resp.status != 200:
                    logger.error(f"Ошибка HTTP инвентаря: {resp.status}")
                    return None

                text = await resp.text()

                # Проверяем на редирект на логин
                if 'login' in text.lower() and 'password' in text.lower():
                    return {'error': 'Требуется вход в аккаунт', 'needauth': True}

                data = await resp.json()
                return data
        except Exception as e:
            logger.error(f"Ошибка инвентаря: {e}")
            raise

    # ==================== ДОБАВИТЬ В КЛАСС AsyncSteamMobile (штучки для трейда) ====================

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
                logger.info(f"   {cookie.key}: {cookie.value[:30]}... (expires: {cookie.get('expires', 'session')})")

        async def do_request():
            async with self.session.get(
                'https://steamcommunity.com/mobileconf/getlist',
                params=params,
                headers=headers,
                ssl=False
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

                # Логируем первые 500 символов ОБЯЗАТЕЛЬНО
                logger.info(f"   📄 Response preview: {text[:500]}")

                # Если ответ короткий — логируем полностью
                if len(text) < 2000:
                    logger.info(f"   📄 Full response: {text}")
                else:
                    logger.info(f"   📄 Response (first 2000 chars): {text[:2000]}")
                logger.info(f"{'='*60}")

                try:
                    data = json.loads(text)

                    # 🔥 ДЕТАЛЬНЫЙ РАЗБОР ОТВЕТА
                    logger.info(f"   Parsed JSON keys: {list(data.keys())}")
                    logger.info(f"   success: {data.get('success')}")
                    logger.info(f"   needauth: {data.get('needauth')}")

                    if data.get('success') and data.get('conf'):
                        logger.info(f"   ✅ Got {len(data['conf'])} confirmations")
                        for i, conf in enumerate(data['conf']):
                            logger.info(f"      [{i}] id={conf.get('id')}, type={conf.get('type')}, headline={conf.get('headline', '')[:50]}")
                    elif data.get('needauth'):
                        logger.warning(f"   🔒 NEEDAUTH! Session expired or invalid")
                        logger.warning(f"   Message: {data.get('message', 'no message')}")
                    elif not data.get('success'):
                        logger.warning(f"   ❌ success=False, message={data.get('message', 'no message')}")
                        logger.warning(f"   Full response data: {json.dumps(data, ensure_ascii=False)[:1000]}")

                    return data
                except json.JSONDecodeError as e:
                    logger.error(f"   ❌ JSON parse error: {e}")
                    logger.error(f"   Raw (first 2000): {text[:2000]}")
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

    async def _full_session_recovery(self) -> bool:
        """
        Полное восстановление сессии: обновляет access_token через refresh_token,
        получает новый sessionid и проверяет что сессия работает.
        Возвращает True если удалось, False если нужен полный логин.
        """
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
            logger.info(f"✅ [{self.account_name}] New sessionid obtained: {new_sessionid[:20]}...")
        except Exception as e:
            logger.error(f"❌ [{self.account_name}] Failed to get new sessionid: {e}")
            return False

        logger.info(f"🔄 [{self.account_name}] Verifying session works...")
        try:
            headers = {
                'User-Agent': USER_AGENT_MOBILE,
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            }
            async with self.session.get('https://steamcommunity.com/my/', headers=headers, ssl=False, allow_redirects=False) as resp:
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
                headers=headers,
                ssl=False
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



    async def get_trade_offer_details(self, tradeofferid: str):
        """Получение деталей конкретного трейд-оффера"""
        url = f'https://steamcommunity.com/tradeoffer/{tradeofferid}/'
        params = {'ajax': '1', 'l': 'english'}

        await self._ensure_session()
        async with self.session.get(url, params=params) as resp:
            return await resp.json()

    async def check_session_valid(self) -> bool:
        """Проверяет, валидна ли текущая сессия"""
        await self._ensure_session()

        try:
            async with self.session.get('https://steamcommunity.com/my/', ssl=False) as resp:
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


    # ==================== ДОБАВИТЬ В КЛАСС AsyncSteamMobile ====================

    async def get_trade_offers_page(self, get_received: bool = True, active_only: bool = True, history: bool = False):
        """Получение HTML страницы с трейдами"""

        # Правильно формируем URL
        if get_received:
            url = 'https://steamcommunity.com/my/tradeoffers/'
        else:
            url = 'https://steamcommunity.com/my/tradeoffers/sent/'

        if history:
            url += '?history=1'

        await self._ensure_session()

        # Проверяем sessionid
        try:
            await self._ensure_sessionid()
        except:
            pass

        headers = {
            'User-Agent': USER_AGENT_MOBILE,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Referer': 'https://steamcommunity.com/',
        }

        try:
            async with self.session.get(url, headers=headers, ssl=False) as resp:
                if resp.status != 200:
                    # 🔥 Логируем детали нестандартного ответа
                    try:
                        error_text = await resp.text()
                        logger.error(f"❌ Trade offers page HTTP {resp.status} for {self.account_name}")
                        logger.error(f"Response preview: {error_text[:500]}")
                    except:
                        logger.error(f"❌ Trade offers page HTTP {resp.status} for {self.account_name} (no body)")
                    raise Exception(f"HTTP {resp.status}: Не удалось загрузить страницу трейдов")

                text = await resp.text()

                if 'login' in text.lower() and 'password' in text.lower():
                    logger.warning(f"🔒 Trade offers page redirected to login for {self.account_name}")
                    # 🔥 Логируем кусок HTML для диагностики
                    logger.info(f"HTML preview: {text[:300]}")
                    raise Exception("NEEDAUTH: Требуется вход в аккаунт")

                # 🔥 Логируем успешный ответ кратко
                logger.info(f"✅ Trade offers page loaded for {self.account_name}, HTML length: {len(text)}")

                return text

        except Exception as e:
            if "NEEDAUTH" in str(e):
                raise Exception("NEEDAUTH")
            raise e


    async def parse_trade_offers_list(self, html_text: str):
        """Парсинг списка трейдов с HTML страницы"""
        import re

        tree = html.fromstring(html_text)
        offers = []

        trade_blocks = tree.xpath('//div[contains(@class, "tradeoffer")]')

        for block in trade_blocks:
            # ID трейда
            offer_id = None
            id_attr = block.xpath('.//@id')
            if id_attr:
                match = re.search(r'tradeofferid_(\d+)', id_attr[0])
                if match:
                    offer_id = match.group(1)

            if not offer_id:
                continue

            # Парсим профиль партнера
            partner_id = None
            partner_name = None
            partner_avatar = None

            profile_links = block.xpath('.//div[contains(@class, "tradeoffer_partner")]//a[contains(@href, "/profiles/") or contains(@href, "/id/")]/@href')
            if profile_links:
                profile_url = profile_links[0]
                if '/profiles/' in profile_url:
                    partner_id = profile_url.split('/')[-1]
                elif '/id/' in profile_url:
                    partner_id = profile_url

            # Аватар
            avatar_link = block.xpath('.//div[contains(@class, "tradeoffer_partner")]//a[contains(@class, "playerAvatar")]')
            if avatar_link:
                avatar_img = avatar_link[0].xpath('.//img/@src')
                partner_avatar = avatar_img[0] if avatar_img else None

            # Имя партнера из заголовка
            header_elem = block.xpath('.//div[contains(@class, "tradeoffer_header")]/text()')
            if header_elem:
                header_text = ' '.join(header_elem).strip()
                patterns = [
                    ' offered you a trade:',
                    ' предложил вам обмен:',
                    ' sent you a trade offer:',
                    ' отправил вам предложение обмена:',
                ]
                for pattern in patterns:
                    if pattern in header_text:
                        partner_name = header_text.replace(pattern, '').strip()
                        break
                if not partner_name and header_text:
                    partner_name = header_text

            if not partner_name:
                name_elem = block.xpath('.//div[contains(@class, "tradeoffer_partner")]//a/text()')
                if name_elem:
                    partner_name = name_elem[0].strip()

            # 🔥 ИСПРАВЛЕНО: Парсим количество предметов
            # Предметы партнера (primary)
            their_items = block.xpath('.//div[contains(@class, "tradeoffer_items") and contains(@class, "primary")]//div[contains(@class, "trade_item")]')
            their_items_count = len(their_items)

            # Ваши предметы (secondary)
            your_items = block.xpath('.//div[contains(@class, "tradeoffer_items") and contains(@class, "secondary")]//div[contains(@class, "trade_item")]')
            your_items_count = len(your_items)

            # Статус трейда
            status = 'active'
            status_text = 'Активен'
            accepted_date = None
            protected_until = None

            items_ctn = block.xpath('.//div[contains(@class, "tradeoffer_items_ctn")]')
            if items_ctn:
                ctn_classes = ' '.join(items_ctn[0].xpath('.//@class'))
                if 'inactive' in ctn_classes:
                    banner = block.xpath('.//div[contains(@class, "tradeoffer_items_banner")]')
                    if banner:
                        banner_text_parts = banner[0].xpath('.//text()')
                        banner_text = ' '.join([p.strip() for p in banner_text_parts if p.strip()])

                        if 'Accepted' in banner_text:
                            status = 'accepted'
                            status_text = '✅ Принят'
                        elif 'Canceled' in banner_text:
                            status = 'canceled'
                            status_text = '❌ Отменен'
                        elif 'Declined' in banner_text:
                            status = 'declined'
                            status_text = '❌ Отклонен'
                        elif 'Expired' in banner_text:
                            status = 'expired'
                            status_text = '⌛ Истек'
                        else:
                            status = 'inactive'
                            status_text = '⏸️ Неактивен'

            # Сообщение
            message = ''
            message_elem = block.xpath('.//div[contains(@class, "tradeoffer_message")]//div[@class="quote"]/text()')
            if message_elem:
                message = message_elem[0].strip()

            # Время истечения
            expires_elem = block.xpath('.//div[contains(@class, "tradeoffer_footer")]//text()')
            expires_text = ''
            for text in expires_elem:
                if 'expires' in text.lower():
                    expires_text = text.strip()
                    break

            offers.append({
                'tradeofferid': offer_id,
                'partner_id': partner_id,
                'partner_name': partner_name or 'Unknown',
                'partner_avatar': partner_avatar,
                'message': message,
                'status': status,
                'status_text': status_text,
                'accepted_date': accepted_date,
                'protected_until': protected_until,
                'your_items_count': your_items_count,    # 🔥 Теперь правильно
                'their_items_count': their_items_count,  # 🔥 Теперь правильно
                'expires_text': expires_text,
            })

        logger.info(f"📊 Parsed {len(offers)} trade offers for {self.account_name}")
        if not offers and html_text:
            logger.warning(f"⚠️ No trade offers parsed from HTML for {self.account_name}")
            logger.info(f"HTML preview: {html_text[:500]}")

        return offers  # 🔥 Вот эта строка уже есть в коде

    async def accept_trade_offer(self, tradeofferid: str, partner_id: str = None):
        """Принятие трейд-оффера"""

        # Гарантируем наличие sessionid
        try:
            await self._ensure_sessionid()
        except Exception as e:
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
            return {
                'success': False,
                'error': 'Требуется вход в аккаунт',
                'needauth': True
            }

        # Формируем строку cookies
        cookie_string = '; '.join([
            f"{cookie.key}={cookie.value}"
            for cookie in self.cookie_jar
            if 'steamcommunity.com' in cookie.get('domain', '')
        ])

        headers = {
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'Cookie': cookie_string,
            'Host': 'steamcommunity.com',
            'Origin': 'https://steamcommunity.com',
            'Referer': f'https://steamcommunity.com/tradeoffer/{tradeofferid}/',
            'User-Agent': USER_AGENT_MOBILE,
            'X-Requested-With': 'XMLHttpRequest',
        }

        payload = {
            'sessionid': sessionid,
            'serverid': '1',
            'tradeofferid': str(tradeofferid),
            'partner': str(partner_id) if partner_id else '',
            'captcha': ''
        }

        await self._ensure_session()

        async def do_request():
            async with self.session.post(
                f'https://steamcommunity.com/tradeoffer/{tradeofferid}/accept',
                data=payload,
                headers=headers,
                ssl=False
            ) as resp:
                response_text = await resp.text()

                # 🔥 Проверяем на needauth
                if resp.status in [401, 403]:
                    return {
                        'success': False,
                        'error': 'Требуется вход в аккаунт',
                        'needauth': True
                    }

                # 🔥 Проверяем на редирект
                if 'login' in response_text.lower() and 'steam' in response_text.lower():
                    return {
                        'success': False,
                        'error': 'Требуется вход в аккаунт',
                        'needauth': True
                    }

                # 🔥 Проверяем на 500
                if resp.status == 500:
                    raise Exception("HTTP_500")

                try:
                    result = json.loads(response_text)
                except:
                    result = json.loads(response_text)

                if resp.status != 200:
                    return {
                        'success': False,
                        'error': f"HTTP {resp.status}"
                    }

                if 'strError' in result:
                    return {
                        'success': False,
                        'error': result['strError']
                    }

                return {
                    'success': True,
                    'tradeid': result.get('tradeid'),
                    'needs_mobile_confirmation': result.get('needs_mobile_confirmation', False),
                    'needs_email_confirmation': result.get('needs_email_confirmation', False),
                    'email_domain': result.get('email_domain'),
                }

        try:
            return await do_request()
        except Exception as e:
            if "HTTP_500" in str(e):
                logger.warning(f"HTTP 500 on accept, trying session recovery...")
                self._recovery_attempted = True

                if await self._try_recover_session():
                    logger.info("Session recovered, retrying accept...")
                    self._session_recovered = True
                    try:
                        return await do_request()
                    except Exception as retry_e:
                        return {
                            'success': False,
                            'error': f'Ошибка после восстановления: {str(retry_e)}',
                            'needauth': True
                        }
                else:
                    self._session_recovery_failed = True
                    return {
                        'success': False,
                        'error': 'Ошибка сервера. Требуется вход в аккаунт.',
                        'needauth': True
                    }

            logger.error(f"Error accepting trade: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e)
            }

    async def decline_trade_offer(self, tradeofferid: str):
        """Отклонение входящего трейд-оффера"""

        # Получаем sessionid из cookies
        sessionid = None
        for cookie in self.cookie_jar:
            if cookie.key == 'sessionid' and 'steamcommunity.com' in cookie.get('domain', ''):
                sessionid = cookie.value
                break

        if not sessionid:
            # Пробуем получить sessionid
            await self._ensure_sessionid()
            for cookie in self.cookie_jar:
                if cookie.key == 'sessionid' and 'steamcommunity.com' in cookie.get('domain', ''):
                    sessionid = cookie.value
                    break

        if not sessionid:
            return {
                'success': False,
                'error': 'Требуется вход в аккаунт',
                'needauth': True
            }

        # Формируем строку cookies для заголовка
        cookie_string = '; '.join([
            f"{cookie.key}={cookie.value}"
            for cookie in self.cookie_jar
            if 'steamcommunity.com' in cookie.get('domain', '')
        ])

        headers = {
            'Accept': '*/*',
            'Accept-Language': 'en-US,en;q=0.9',
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'Cookie': cookie_string,
            'Origin': 'https://steamcommunity.com',
            'Referer': f'https://steamcommunity.com/tradeoffer/{tradeofferid}/',
            'User-Agent': USER_AGENT_MOBILE,
            'X-Requested-With': 'XMLHttpRequest',
        }

        payload = {
            'sessionid': sessionid
        }

        await self._ensure_session()

        try:
            logger.info(f"Declining trade offer {tradeofferid}")
            logger.info(f"Using sessionid: {sessionid[:10]}...")

            url = f'https://steamcommunity.com/tradeoffer/{tradeofferid}/decline'

            async with self.session.post(
                url,
                data=payload,
                headers=headers,
                ssl=False
            ) as resp:

                logger.info(f"Decline trade response status: {resp.status}")

                response_text = await resp.text()
                logger.info(f"Decline trade response: {response_text[:200]}")

                # 🔥 ПРОВЕРЯЕМ НА NEEDAUTH
                if resp.status in [401, 403, 429]:
                    logger.warning(f"Got {resp.status}, need auth for trade {tradeofferid}")
                    return {
                        'success': False,
                        'error': 'Требуется вход в аккаунт',
                        'needauth': True
                    }

                # 🔥 Проверяем на редирект на страницу логина
                if resp.status == 200 and ('login' in response_text.lower() and 'password' in response_text.lower()):
                    logger.warning(f"Redirected to login page for trade {tradeofferid}")
                    return {
                        'success': False,
                        'error': 'Требуется вход в аккаунт',
                        'needauth': True
                    }

                # 🔥 Проверяем на ошибку 500
                if resp.status == 500:
                    logger.error(f"HTTP 500 for trade {tradeofferid}")
                    # Пробуем восстановить сессию
                    self._recovery_attempted = True

                    if await self._try_recover_session():
                        logger.info("Session recovered after 500 error, retrying...")
                        # Повторяем запрос
                        async with self.session.post(
                            url,
                            data=payload,
                            headers=headers,
                            ssl=False
                        ) as resp2:
                            response_text2 = await resp2.text()

                            if resp2.status == 200:
                                try:
                                    result = json.loads(response_text2)
                                    if result.get('tradeofferid') == tradeofferid:
                                        return {
                                            'success': True,
                                            'tradeofferid': result.get('tradeofferid')
                                        }
                                except:
                                    pass

                            return {
                                'success': False,
                                'error': f'HTTP {resp2.status} после восстановления сессии',
                                'needauth': True if resp2.status in [401, 403] else False
                            }
                    else:
                        self._session_recovery_failed = True
                        return {
                            'success': False,
                            'error': 'Ошибка сервера. Возможно, требуется повторный вход в аккаунт.',
                            'needauth': True
                        }

                # Парсим ответ
                try:
                    result = json.loads(response_text)
                except:
                    if 'tradeofferid' in response_text and tradeofferid in response_text:
                        return {
                            'success': True,
                            'tradeofferid': tradeofferid
                        }
                    return {
                        'success': False,
                        'error': f'Неверный ответ: {response_text[:200]}'
                    }

                if resp.status != 200:
                    return {
                        'success': False,
                        'error': f"HTTP {resp.status}: {result.get('strError', response_text[:100])}"
                    }

                if result.get('tradeofferid') == tradeofferid:
                    return {
                        'success': True,
                        'tradeofferid': result.get('tradeofferid')
                    }
                else:
                    return {
                        'success': False,
                        'error': result.get('strError', 'Unexpected response'),
                        'raw_response': result
                    }

        except Exception as e:
            logger.error(f"Error declining trade: {e}", exc_info=True)
            return {
                'success': False,
                'error': str(e)
            }

    async def cancel_trade_offer(self, tradeofferid: str):
        """Отмена исходящего трейд-оффера"""
        sessionid = None
        for cookie in self.cookie_jar:
            if cookie.key == 'sessionid' and 'steamcommunity.com' in cookie.get('domain', ''):
                sessionid = cookie.value
                break

        if not sessionid:
            raise Exception("SessionID not found")

        headers = {
            'Referer': f'https://steamcommunity.com/tradeoffer/{tradeofferid}',
            'X-Requested-With': 'XMLHttpRequest',
            'Origin': 'https://steamcommunity.com',
        }

        payload = {
            'sessionid': sessionid,
            'serverid': '1',
            'tradeofferid': str(tradeofferid),
        }

        await self._ensure_session()
        async with self.session.post(
            f'https://steamcommunity.com/tradeoffer/{tradeofferid}/cancel',
            data=payload,
            headers=headers,
            ssl=False
        ) as resp:
            return await resp.json()

    async def parse_trade_offer_page_full(self, tradeofferid: str):
        """Парсинг страницы трейда для получения детальной информации"""
        import re
        import json as json_module

        url = f'https://steamcommunity.com/tradeoffer/{tradeofferid}/'

        await self._ensure_session()
        headers = {
            'User-Agent': USER_AGENT_MOBILE,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        }

        async with self.session.get(url, headers=headers, ssl=False) as resp:
            html_content = await resp.text()

        tree = html.fromstring(html_content)

        result = {
            'tradeofferid': tradeofferid,
            'partner_name': None,
            'partner_steamid': None,
            'partner_avatar': None,
            'partner_level': None,
            'level_color': '#808080',
            'partner_member_since': None,
            'friends_since': None,
            'badge': None,
            'badge_url': None,
            'message': None,
            'your_items_count': 0,
            'their_items_count': 0,
        }

        # 🔥 ПАРСИМ JavaScript переменную g_rgCurrentTradeStatus для получения точного количества предметов
        trade_status_match = re.search(r'var\s+g_rgCurrentTradeStatus\s*=\s*({.*?});', html_content, re.DOTALL)
        if trade_status_match:
            try:
                trade_status_json = trade_status_match.group(1)
                # Исправляем возможные проблемы с JSON (trailing commas, single quotes)
                trade_status_json = re.sub(r',\s*}', '}', trade_status_json)
                trade_status_json = re.sub(r',\s*]', ']', trade_status_json)

                trade_status = json_module.loads(trade_status_json)

                me_assets = trade_status.get('me', {}).get('assets', [])
                them_assets = trade_status.get('them', {}).get('assets', [])

                # Считаем общее количество предметов (учитывая amount)
                your_items_count = sum(int(asset.get('amount', 1)) for asset in me_assets)
                their_items_count = sum(int(asset.get('amount', 1)) for asset in them_assets)

                result['your_items_count'] = your_items_count
                result['their_items_count'] = their_items_count

                logger.info(f"Parsed from g_rgCurrentTradeStatus: your_items={your_items_count}, their_items={their_items_count}")
            except Exception as e:
                logger.error(f"Failed to parse g_rgCurrentTradeStatus: {e}")
                # Fallback to XPath
                your_items = tree.xpath('//div[@id="trade_yours"]//div[contains(@class, "trade_item")]')
                their_items = tree.xpath('//div[@id="trade_theirs"]//div[contains(@class, "trade_item")]')
                result['your_items_count'] = len(your_items)
                result['their_items_count'] = len(their_items)
        else:
            # Fallback to XPath
            your_items = tree.xpath('//div[@id="trade_yours"]//div[contains(@class, "trade_item")]')
            their_items = tree.xpath('//div[@id="trade_theirs"]//div[contains(@class, "trade_item")]')
            result['your_items_count'] = len(your_items)
            result['their_items_count'] = len(their_items)

        # 1. Имя партнера и ссылка на профиль
        partner_elem = tree.xpath('//div[contains(@class, "trade_partner_headline_sub")]//a')
        if not partner_elem:
            partner_elem = tree.xpath('//div[contains(@class, "trade_partner_headline")]//a[contains(@href, "/profiles/") or contains(@href, "/id/")]')

        if partner_elem:
            result['partner_name'] = partner_elem[0].text_content().strip()
            href = partner_elem[0].get('href', '')
            if href:
                if '/profiles/' in href:
                    result['partner_steamid'] = href.split('/')[-1]
                elif '/id/' in href:
                    # Для кастомных URL пробуем найти SteamID в JavaScript
                    steamid_match = re.search(r"var\s+g_ulTradePartnerSteamID\s*=\s*'(\d+)'", html_content)
                    if steamid_match:
                        result['partner_steamid'] = steamid_match.group(1)
                    else:
                        result['partner_steamid'] = href.split('/')[-1]

        # 2. Аватар партнера - ищем в offerheader блока trade_theirs
        avatar_elem = tree.xpath('//div[@id="trade_theirs"]//div[contains(@class, "offerheader")]//img/@src')
        if not avatar_elem:
            avatar_elem = tree.xpath('//div[contains(@class, "trade_partner_header")]//img[contains(@class, "playerAvatar")]/@src')
        if not avatar_elem:
            avatar_elem = tree.xpath('//div[contains(@class, "trade_partner_icon")]//img/@src')

        if avatar_elem:
            result['partner_avatar'] = avatar_elem[0]
            logger.info(f"Found partner avatar: {avatar_elem[0][:60]}...")
        else:
            logger.warning(f"No partner avatar found for trade {tradeofferid}")

        # 3. Steam Level
        level_elem = tree.xpath('//div[contains(@class, "friendPlayerLevel")]')
        if level_elem:
            level_num = level_elem[0].xpath('.//span[@class="friendPlayerLevelNum"]/text()')
            if level_num:
                result['partner_level'] = level_num[0].strip()

            class_attr = level_elem[0].get('class', '')
            lvl_match = re.search(r'lvl_(\d+)', class_attr)
            if lvl_match:
                lvl_class = f"lvl_{lvl_match.group(1)}"
                style_texts = tree.xpath('//style/text()')
                for style_text in style_texts:
                    pattern = rf'\.friendPlayerLevel\.{lvl_class}\s*\{{\s*border-color:\s*([#][0-9a-fA-F]{{6}})'
                    match = re.search(pattern, style_text, re.IGNORECASE)
                    if match:
                        result['level_color'] = match.group(1).upper()
                        break

        # 4. Друзья с
        friends_blocks = tree.xpath('//div[contains(@class, "trade_partner_info_block")]')
        for block in friends_blocks:
            block_text = ' '.join(block.xpath('.//text()'))
            if 'friends since' in block_text.lower():
                date_elem = block.xpath('.//div[contains(@class, "trade_partner_info_text")]/text()')
                if date_elem:
                    result['friends_since'] = date_elem[0].strip()
                break

        # 5. Дата регистрации (Member since)
        member_since_elem = tree.xpath('//div[contains(@class, "trade_partner_member_since")]/text()')
        if member_since_elem:
            result['partner_member_since'] = member_since_elem[0].strip()

        # 6. Бейдж (years of service)
        badge_img = tree.xpath('//div[contains(@class, "years_of_service")]//img/@src')
        if badge_img:
            result['badge_url'] = badge_img[0]
            match = re.search(r'steamyears(\d+)_', badge_img[0])
            if match:
                result['badge'] = f"{match.group(1)} years of service"

        # 7. Комментарий к трейду
        message_elem = tree.xpath('//div[contains(@class, "included_trade_offer_note")]/text()')
        if message_elem:
            msg = ' '.join(message_elem).strip()
            if msg and msg != '<none>':
                result['message'] = msg

        logger.info(f"Parsed trade {tradeofferid}: partner={result['partner_name']}, "
                    f"your_items={result['your_items_count']}, their_items={result['their_items_count']}")

        return result


    async def generate_trade_image(self, trade_info: dict):
        """Генерация изображения трейда с динамическим цветом уровня"""

        # Создаём холст 570x320, фон #1b2838
        img = Image.new('RGB', (570, 320), color='#1b2838')
        draw = ImageDraw.Draw(img)

        # Шрифты
        try:
            font_title = ImageFont.truetype("arialbd.ttf", 18)
            font_normal = ImageFont.truetype("arial.ttf", 15)
        except:
            font_title = ImageFont.load_default()
            font_normal = ImageFont.load_default()

        partner_name = trade_info.get('partner_name') or trade_info.get('partner_steamid', 'Unknown')
        level_color = trade_info.get('level_color', '#76adcc')

        logger.info(f"Generating trade image for {partner_name}, level color: {level_color}")

        # ========== ЗАГОЛОВОК ==========
        y = 25
        x = 25

        # "This Trade:" (#76adcc)
        draw.text((x, y), "This Trade:", fill='#76adcc', font=font_title)
        this_trade_width = draw.textlength("This Trade:", font=font_title)

        # " You are trading with " (белый) + Nickname (белый)
        draw.text((x + this_trade_width, y), f" You are trading with {partner_name}",
                fill='#ffffff', font=font_title)

        # Отступ 15 пикселей после заголовка
        y += 40  # 25 + 15 = 40

        # ========== БЛОК 1: ДРУЗЬЯ ==========
        block1_height = 65

        # Фон блока #314055
        draw.rectangle((25, y, 545, y + block1_height), fill='#314055')

        # Аватарка
        avatar_size = 45
        avatar_x = 35
        avatar_y = y + (block1_height - avatar_size) // 2

        avatar_url = trade_info.get('partner_avatar')
        if avatar_url:
            try:
                async with self.session.get(avatar_url, ssl=False, headers={'User-Agent': USER_AGENT_MOBILE}) as resp:
                    if resp.status == 200:
                        avatar_data = await resp.read()
                        avatar_img = Image.open(io.BytesIO(avatar_data))
                        avatar_img = avatar_img.resize((avatar_size, avatar_size))

                        # Круглая маска
                        mask = Image.new('L', (avatar_size, avatar_size), 0)
                        mask_draw = ImageDraw.Draw(mask)
                        mask_draw.ellipse((0, 0, avatar_size, avatar_size), fill=255)

                        img.paste(avatar_img, (avatar_x, avatar_y), mask)
                        logger.info("Avatar pasted successfully")
            except Exception as e:
                logger.warning(f"Failed to load avatar: {e}")

        # Текст друзей
        text_x = avatar_x + avatar_size + 15
        text_y = y + block1_height // 2 - 8

        if trade_info.get('friends_since'):
            friends_text = f"You've been friends since {trade_info['friends_since']}"
        else:
            friends_text = "Not friends"

        draw.text((text_x, text_y), friends_text, fill='#76adcc', font=font_normal)

        # Отступ 15 пикселей между блоками
        y += block1_height + 15

        # ========== БЛОК 2: STEAM LEVEL ==========
        block2_height = 65

        # Фон блока #314055
        draw.rectangle((25, y, 545, y + block2_height), fill='#314055')

        # Круг с уровнем (используем спарсенный цвет)
        circle_size = 45
        circle_x = 35
        circle_y = y + (block2_height - circle_size) // 2

        draw.ellipse((circle_x, circle_y, circle_x + circle_size, circle_y + circle_size),
                    outline=level_color, width=2)

        # Уровень в центре круга
        level = trade_info.get('partner_level', '?')
        bbox = draw.textbbox((0, 0), str(level), font=font_title)
        level_width = bbox[2] - bbox[0]
        level_height = bbox[3] - bbox[1]
        level_x = circle_x + (circle_size - level_width) // 2
        level_y = circle_y + (circle_size - level_height) // 2 - 2

        draw.text((level_x, level_y), str(level), fill='#ffffff', font=font_title)

        # Текст справа от круга
        text_x = circle_x + circle_size + 15
        text_y = y + block2_height // 2 - 8

        draw.text((text_x, text_y), partner_name, fill='#ffffff', font=font_normal)
        name_width = draw.textlength(partner_name, font=font_normal)

        level_text = f" has a Steam Level of {level}"
        draw.text((text_x + name_width, text_y), level_text, fill='#76adcc', font=font_normal)

        # Отступ 15 пикселей между блоками
        y += block2_height + 15

        # ========== БЛОК 3: ДАТА РЕГИСТРАЦИИ ==========
        block3_height = 65

        # Фон блока #314055
        draw.rectangle((25, y, 545, y + block3_height), fill='#314055')

        # Бейджик
        badge_size = 45
        badge_x = 35
        badge_y = y + (block3_height - badge_size) // 2

        badge_url = trade_info.get('badge_url')
        if badge_url:
            try:
                async with self.session.get(badge_url, ssl=False, headers={'User-Agent': USER_AGENT_MOBILE}) as resp:
                    if resp.status == 200:
                        badge_data = await resp.read()
                        badge_img = Image.open(io.BytesIO(badge_data))
                        badge_img = badge_img.resize((badge_size, badge_size))
                        img.paste(badge_img, (badge_x, badge_y))
                        logger.info("Badge pasted successfully")
            except Exception as e:
                logger.warning(f"Failed to load badge: {e}")

        # Текст справа от бейджа
        text_x = badge_x + badge_size + 15
        text_y = y + block3_height // 2 - 8

        draw.text((text_x, text_y), partner_name, fill='#ffffff', font=font_normal)
        name_width = draw.textlength(partner_name, font=font_normal)

        member_since = trade_info.get('partner_member_since', 'unknown date')
        since_text = f" has been on Steam since {member_since}"
        draw.text((text_x + name_width, text_y), since_text, fill='#76adcc', font=font_normal)

        # Сохраняем
        output = io.BytesIO()
        img.save(output, format='PNG')

        logger.info(f"Trade image generated successfully for {partner_name}")
        return output.getvalue()

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
                headers=headers,
                ssl=False
            ) as resp:
                response_text = await resp.text()
                logger.info(f"📥 Response: {response_text[:200]}")

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
