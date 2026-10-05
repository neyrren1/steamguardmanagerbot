"""Steam inventory retrieval operations."""

import json
from typing import Dict, List

from guardbot.config import logger
from guardbot.steam.constants import USER_AGENT_MOBILE


class InventoryMixin:
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
            async with self.session.get(url, headers=headers) as resp:
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
                    await resp.read()
                    logger.error(f"❌ Inventory returned non-JSON for {self.account_name}: Content-Type={content_type}")
                    logger.error("Inventory response body omitted from logs")
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
            async with self.session.get(url, headers=headers, params=params) as resp:
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
