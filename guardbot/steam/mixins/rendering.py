"""Trade rendering and Steam CDN asset retrieval."""

import io
from typing import Protocol, cast

from PIL import Image, ImageDraw, ImageFont

from guardbot.config import logger
from guardbot.steam.constants import USER_AGENT_MOBILE


class _AssetValidator(Protocol):
    def _validate_steam_asset_url(self, asset_url: str) -> str: ...


class TradeRenderingMixin:
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
                avatar_data = await self._download_steam_asset(avatar_url)
                if avatar_data:
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
                badge_data = await self._download_steam_asset(badge_url)
                if badge_data:
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

    async def _download_steam_asset(self, asset_url: str) -> bytes:
        """Download a bounded image through the mandatory Steam proxy transport."""
        validator = cast(_AssetValidator, self)
        safe_url = validator._validate_steam_asset_url(asset_url)
        resp = await self._request(
            'GET',
            safe_url,
            headers={'User-Agent': USER_AGENT_MOBILE},
            allow_redirects=False,
            max_response_bytes=2 * 1024 * 1024,
        )
        if resp.status != 200:
            return b''
        content_type = (resp.headers.get('Content-Type') or '').lower()
        if not content_type.startswith('image/'):
            raise ValueError("Steam CDN вернул не изображение")
        return await resp.read()
