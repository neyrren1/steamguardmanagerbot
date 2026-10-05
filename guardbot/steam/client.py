"""Public Steam client facade and proxy-enforced HTTP session."""

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import socket
import struct
import time
import uuid
from base64 import b64encode
from datetime import datetime, timezone
from typing import Dict, List, Optional

import aiohttp
from aiohttp_socks import ProxyConnector
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.asymmetric import rsa as crypto_rsa
from yarl import URL

from guardbot.config import logger
from guardbot.steam.assets import validate_steam_asset_url as validate_steam_asset_url
from guardbot.steam.constants import USER_AGENT_MOBILE
from guardbot.steam.mixins import (
    ConfirmationsMixin,
    InventoryMixin,
    MarketMixin,
    ProfileMixin,
    SessionRecoveryMixin,
    TradeOffersMixin,
    TradeRenderingMixin,
)

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


class ProxyRequiredError(RuntimeError):
    """Raised when a Steam network operation has no configured proxy."""

# ============================================================
# STEAM MOBILE CLIENT - СЕССИЯ (ИСПРАВЛЕННАЯ)
# ============================================================

PROXY_DNS_TIMEOUT_SECONDS = 10.0

def parse_proxy_string(proxy_str: str | None) -> str | None:
    """
    Проверяет и исправляет формат прокси
    Поддерживает: http://, socks4://, socks5://
    """
    if not proxy_str or not proxy_str.strip():
        return None

    proxy_str = proxy_str.strip()

    if '://' not in proxy_str:
        # Preserve the legacy SOCKS5 shorthand ports; other ports default to HTTP.
        port_hint = proxy_str.rsplit(':', 1)[-1]
        default_scheme = 'socks5' if port_hint in {'1080', '1081', '46281'} else 'http'
        proxy_str = f'{default_scheme}://{proxy_str}'

    try:
        parsed = URL(proxy_str)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("Некорректный URL прокси") from exc

    # aiohttp-socks rejects HTTPS proxy URLs, so fail during validation rather
    # than later with a confusing connector error.
    if parsed.scheme not in {'http', 'socks4', 'socks5'}:
        raise ValueError("Неподдерживаемый протокол прокси")
    if not parsed.host or port is None or not 1 <= port <= 65535:
        raise ValueError("Прокси должен содержать хост и корректный порт")
    if parsed.path not in {'', '/'} or parsed.query_string or parsed.fragment:
        raise ValueError("URL прокси не должен содержать путь, query или fragment")

    host = parsed.host.rstrip('.').lower()
    if host == 'localhost' or host.endswith(('.localhost', '.local')):
        raise ValueError("Локальные адреса нельзя использовать как прокси")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValueError("Приватные и служебные адреса нельзя использовать как прокси")

    return str(parsed)


async def resolve_public_proxy_url(proxy_url: str) -> str:
    """Resolve a proxy once, reject non-global IPs, and pin the connection."""
    normalized = parse_proxy_string(proxy_url)
    if normalized is None:
        raise ProxyRequiredError("Прокси не настроен")
    parsed = URL(normalized)
    host = parsed.host
    if host is None:
        raise ValueError("Прокси должен содержать хост")

    try:
        literal_address = ipaddress.ip_address(host)
    except ValueError:
        literal_address = None
    if literal_address is not None:
        return normalized

    try:
        results = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                host,
                parsed.port,
                type=socket.SOCK_STREAM,
            ),
            timeout=PROXY_DNS_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        raise ValueError("Таймаут DNS-разрешения хоста прокси") from exc
    except OSError as exc:
        raise ValueError("Не удалось разрешить имя хоста прокси") from exc

    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for result in results:
        try:
            address = ipaddress.ip_address(str(result[4][0]).split("%", 1)[0])
        except (IndexError, ValueError) as exc:
            raise ValueError("DNS вернул некорректный адрес прокси") from exc
        if not address.is_global:
            raise ValueError("DNS прокси указывает на приватный или служебный адрес")
        addresses.add(address)
    if not addresses:
        raise ValueError("DNS не вернул адрес прокси")

    # Connecting to the chosen address prevents a second DNS lookup and DNS
    # rebinding between validation and the actual proxy connection.
    pinned_address = min(addresses, key=lambda address: (address.version, int(address)))
    return str(parsed.with_host(str(pinned_address)))


async def probe_proxy_external_ip(proxy_url: str) -> str:
    """Test a proxy through a DNS-pinned transport and return its reported IP."""
    pinned_proxy = await resolve_public_proxy_url(proxy_url)
    timeout = aiohttp.ClientTimeout(total=30)
    connector = (
        ProxyConnector.from_url(pinned_proxy, rdns=True)
        if pinned_proxy.startswith(("socks5://", "socks4://"))
        else None
    )
    if connector is not None:
        async with aiohttp.ClientSession(
            timeout=timeout, connector=connector
        ) as session:
            async with session.get("https://api.ipify.org?format=json") as response:
                response.raise_for_status()
                body = await response.content.read(4097)
    else:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                "https://api.ipify.org?format=json", proxy=pinned_proxy
            ) as response:
                response.raise_for_status()
                body = await response.content.read(4097)
    if len(body) > 4096:
        raise ValueError("Ответ проверки прокси слишком большой")
    try:
        data = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Сервис проверки прокси вернул некорректный ответ") from exc
    external_ip = data.get("ip") if isinstance(data, dict) else None
    if not isinstance(external_ip, str) or not external_ip or len(external_ip) > 64:
        raise ValueError("Сервис проверки прокси не вернул IP-адрес")
    return external_ip


def redact_proxy_url(proxy_url: str) -> str:
    """Return a proxy URL safe for logs and Telegram messages."""
    parsed = URL(proxy_url)
    if parsed.password is None:
        return str(parsed)
    user = parsed.user or ''
    credentials = f"{user}:***@" if user else "***@"
    return f"{parsed.scheme}://{credentials}{parsed.raw_host}:{parsed.port}"



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
        self._proxy_connector: ProxyConnector | None = None
        self._session_proxy: str | None = None
        self._cookie_jar: aiohttp.CookieJar | None = None

    @property
    def cookie_jar(self) -> aiohttp.CookieJar:
        return self._ensure_cookie_jar()

    def _ensure_cookie_jar(self) -> aiohttp.CookieJar:
        if self._cookie_jar is None:
            self._cookie_jar = aiohttp.CookieJar()
        return self._cookie_jar

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def _ensure_session(self):
        if not self.proxy:
            raise ProxyRequiredError(
                "Прокси не настроен. Сетевые запросы к Steam без прокси запрещены."
            )

        # 🔥 ВСЕГДА пересоздаём сессию, если она без прокси или прокси изменился
        if self.session and not self.session.closed:
            if (
                self.session.connector is not self._proxy_connector
                or self._session_proxy != self.proxy
            ):
                logger.warning("Недоверенная HTTP-сессия заменена на proxy-only сессию")
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

            logger.info("Создаю ProxyConnector для %s", redact_proxy_url(self.proxy))
            pinned_proxy = await resolve_public_proxy_url(self.proxy)
            connector = ProxyConnector.from_url(pinned_proxy, rdns=True)
            self._proxy_connector = connector
            self._session_proxy = self.proxy

            self.session = aiohttp.ClientSession(
                headers=headers,
                cookie_jar=self._ensure_cookie_jar(),
                connector=connector,
                timeout=timeout
            )
            logger.info(f"✅ Сессия создана с ProxyConnector")

    async def close(self):
        """Закрытие сессии"""
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None
        self._proxy_connector = None
        self._session_proxy = None

    async def _request(
        self,
        method: str,
        url: str,
        *,
        max_response_bytes: int | None = None,
        **kwargs,
    ):
        """
        Выполняет HTTP запрос с поддержкой прокси
        """

        if not self.proxy:
            raise ProxyRequiredError("Сетевые запросы к Steam без прокси запрещены")

        await self._ensure_session()

        if self.session.connector is not self._proxy_connector:
            raise ProxyRequiredError("HTTP-сессия не привязана к настроенному прокси")

        # Логируем прокси
        logger.info("Steam request: %s %s via %s", method, url, redact_proxy_url(self.proxy))

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

        try:
            async with self.session.request(method, url, **kwargs) as resp:
                if max_response_bytes is None:
                    await resp.read()
                else:
                    content_length = resp.content_length
                    if content_length is not None and content_length > max_response_bytes:
                        raise ValueError("Ответ Steam CDN превышает допустимый размер")
                    payload = await resp.content.read(max_response_bytes + 1)
                    if len(payload) > max_response_bytes:
                        raise ValueError("Ответ Steam CDN превышает допустимый размер")
                    resp._body = payload
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
        await self._ensure_session()

        # Проверяем, есть ли уже sessionid
        for cookie in self.cookie_jar:
            if cookie.key == 'sessionid' and 'steamcommunity.com' in cookie.get('domain', ''):
                logger.info(f"✅ [{self.account_name}] sessionid already exists")
                self.session_id = cookie.value
                return cookie.value

        # Если нет - получаем через ОДИН запрос к главной странице
        logger.info(f"🔄 [{self.account_name}] sessionid not found, fetching from Steam Community...")

        headers = {
            'User-Agent': USER_AGENT_MOBILE,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        }

        try:
            async with self.session.get('https://steamcommunity.com/', headers=headers) as resp:
                logger.info(f"📡 [{self.account_name}] Steam Community response: HTTP {resp.status}")

                response_text = await resp.text()

                # Логируем ВСЕ куки которые пришли
                logger.info(f"🍪 [{self.account_name}] Cookies after request:")
                new_sessionid = None
                for cookie in self.cookie_jar:
                    logger.info(f"   cookie present: {cookie.key} (domain: {cookie.get('domain', '')})")
                    if cookie.key == 'sessionid' and 'steamcommunity.com' in cookie.get('domain', ''):
                        new_sessionid = cookie.value

                if new_sessionid:
                    logger.info(f"✅ [{self.account_name}] Got new sessionid")
                    self.session_id = new_sessionid
                    return new_sessionid
                else:
                    logger.error(f"❌ [{self.account_name}] sessionid NOT found in response cookies!")
                    # 🔥 Проверяем — может редирект на логин?
                    if 'login' in response_text.lower() or 'steam_openid' not in response_text:
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

        if not self.proxy:
            self.__steamTimeAligned = True
            self.__steamTimeDiff = 0
            self.__steamTimeSyncedWithProxy = False
            logger.info("Прокси не настроен: Steam Guard использует только локальное время")
            return

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

        cookie_jar = self._ensure_cookie_jar()
        for cookie_dict in cookies_list:
            try:
                cookie_jar.update_cookies(
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
            await resp.read()
            logger.error("finalizelogin returned a non-JSON response")
            return None

        if not res.get('success', True):
            logger.error("finalizelogin failed")
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

class AsyncSteamMobile(
    AsyncSteamSession,
    SessionRecoveryMixin,
    ProfileMixin,
    InventoryMixin,
    ConfirmationsMixin,
    MarketMixin,
    TradeOffersMixin,
    TradeRenderingMixin,
):
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

    def _validate_steam_asset_url(self, asset_url: str) -> str:
        """Resolve the legacy facade patch point for Steam CDN validation."""
        return validate_steam_asset_url(asset_url)

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
