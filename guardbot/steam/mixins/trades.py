"""Steam trade-offer retrieval, parsing, and actions."""

import json

from lxml import html

from guardbot.config import logger
from guardbot.steam.constants import USER_AGENT_MOBILE


class TradeOffersMixin:
    async def get_trade_offer_details(self, tradeofferid: str):
        """Получение деталей конкретного трейд-оффера"""
        url = f'https://steamcommunity.com/tradeoffer/{tradeofferid}/'
        params = {'ajax': '1', 'l': 'english'}

        await self._ensure_session()
        async with self.session.get(url, params=params) as resp:
            return await resp.json()

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
            async with self.session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    # 🔥 Логируем детали нестандартного ответа
                    try:
                        await resp.read()
                        logger.error(f"❌ Trade offers page HTTP {resp.status} for {self.account_name}")
                        logger.error("Trade offers response body omitted from logs")
                    except:
                        logger.error(f"❌ Trade offers page HTTP {resp.status} for {self.account_name} (no body)")
                    raise Exception(f"HTTP {resp.status}: Не удалось загрузить страницу трейдов")

                text = await resp.text()

                if 'login' in text.lower() and 'password' in text.lower():
                    logger.warning(f"🔒 Trade offers page redirected to login for {self.account_name}")
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
            logger.info("Parsing trade offers HTML (%s bytes)", len(html_text))

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
                headers=headers
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
            logger.info(f"Using sessionid for [{self.account_name}]")

            url = f'https://steamcommunity.com/tradeoffer/{tradeofferid}/decline'

            async with self.session.post(
                url,
                data=payload,
                headers=headers
            ) as resp:

                logger.info(f"Decline trade response status: {resp.status}")

                response_text = await resp.text()
                logger.info("Decline trade response received (%s bytes)", len(response_text))

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
                            headers=headers
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
            headers=headers
        ) as resp:
            return await resp.json()

    async def parse_trade_offer_page_full(self, tradeofferid: str):
        """Парсинг страницы трейда для получения детальной информации"""
        import json as json_module
        import re

        url = f'https://steamcommunity.com/tradeoffer/{tradeofferid}/'

        await self._ensure_session()
        headers = {
            'User-Agent': USER_AGENT_MOBILE,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
        }

        async with self.session.get(url, headers=headers) as resp:
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
