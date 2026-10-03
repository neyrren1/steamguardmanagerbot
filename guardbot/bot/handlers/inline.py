"""Extracted from the legacy bot module without behavior changes."""

from aiogram.types import InlineQuery, InlineQueryResultArticle, InputTextMessageContent
from guardbot.bot.runtime import dp, process_inventory_items_universal
from guardbot.config import logger
from guardbot.database import AccountGroup, AsyncSessionLocal, Mafile
from guardbot.security import decrypt_value
from guardbot.services.session_manager import SteamSessionManager
from sqlalchemy import func, select, text
import base64
import hashlib
import hmac
import math
import struct
import time

# ==================== ИНЛАЙН РЕЖИМ ====================


@dp.inline_query()
async def inline_handler(inline_query: InlineQuery):
    """Обработчик инлайн-запросов"""
    user_id = inline_query.from_user.id
    query = inline_query.query.strip()
    query_lower = query.lower()

    # ========== ПУСТОЙ ЗАПРОС ==========
    if not query:
        help_results = [
            InlineQueryResultArticle(
                id="help_gc",
                title="🔐 gc — Получить Steam Guard код",
                description="Введите @steamguardmanager_bot gc логин",
                input_message_content=InputTextMessageContent(
                    message_text="🔐 <b>Steam Guard коды</b>\n\n"
                                 "Введите <code>@steamguardmanager_bot gc</code> — все аккаунты\n"
                                 "Введите <code>@steamguardmanager_bot gc логин</code> — поиск",
                    parse_mode="HTML"
                ),
            ),
            InlineQueryResultArticle(
                id="help_tl",
                title="🔗 tl — Получить трейд-ссылку",
                description="Введите @steamguardmanager_bot tl логин",
                input_message_content=InputTextMessageContent(
                    message_text="🔗 <b>Трейд-ссылки</b>\n\n"
                                 "Введите <code>@steamguardmanager_bot tl логин</code> — поиск\n"
                                 "Ссылка появится когда останется один вариант",
                    parse_mode="HTML"
                ),
            ),
            InlineQueryResultArticle(
                id="help_sid",
                title="🆔 sid — Показать SteamID",
                description="Введите @steamguardmanager_bot sid",
                input_message_content=InputTextMessageContent(
                    message_text="🆔 <b>SteamID</b>\n\n"
                                 "Введите <code>@steamguardmanager_bot sid</code> для получения SteamID",
                    parse_mode="HTML"
                ),
            ),
            InlineQueryResultArticle(
                id="help_sl",
                title="🔗 sl — Ссылка на профиль Steam",
                description="Введите @steamguardmanager_bot sl логин",
                input_message_content=InputTextMessageContent(
                    message_text="🔗 <b>Ссылка на профиль</b>\n\n"
                                 "Введите <code>@steamguardmanager_bot sl логин</code> — поиск\n"
                                 "Ссылка появится когда останется один вариант",
                    parse_mode="HTML"
                ),
            ),
            InlineQueryResultArticle(
                id="help_group",
                title="📂 group — Показать группу",
                description="Введите @steamguardmanager_bot group название",
                input_message_content=InputTextMessageContent(
                    message_text="📂 <b>Группы аккаунтов</b>\n\n"
                                 "Введите <code>@steamguardmanager_bot group</code> — список групп\n"
                                 "Введите <code>@steamguardmanager_bot group название</code> — поиск",
                    parse_mode="HTML"
                ),
            ),
            InlineQueryResultArticle(
                id="help_inv",
                title="🎒 inv — Просмотр инвентаря",
                description="Введите @steamguardmanager_bot inv cs логин",
                input_message_content=InputTextMessageContent(
                    message_text="🎒 <b>Инвентарь</b>\n\n"
                                "Введите <code>@steamguardmanager_bot inv cs логин</code>\n"
                                "Игры: cs, tf, dota, steam",
                    parse_mode="HTML"
                ),
            ),
        ]
        await inline_query.answer(help_results, cache_time=10)
        return

    # ========== ОПРЕДЕЛЯЕМ КОМАНДУ И АРГУМЕНТ ==========
    command = None
    argument = None

    # Проверяем префиксы команд (с пробелом — значит есть аргумент)
    prefix_patterns = {
        'gc': ['/gc ', 'gc ', 'код ', '/getcode ', 'getcode ', '/guardcode ', 'guardcode '],
        'tl': ['/tl ', 'tl ', '/tradelink ', 'tradelink '],
        'sid': ['/sid ', 'sid ', '/steamid ', 'steamid '],
        'sl': ['/sl ', 'sl ', '/steamlink ', 'steamlink ', '/profile ', 'profile '],
        'group': ['/group ', 'group ', '/showgroup ', 'showgroup ', '/группа ', 'группа '],
        'inv': ['/inv ', 'inv ', '/inventory ', 'inventory ', '/инвентарь ', 'инвентарь '],
    }

    # Сначала ищем с аргументом
    for cmd, prefixes in prefix_patterns.items():
        for prefix in prefixes:
            if query_lower.startswith(prefix):
                command = cmd
                argument = query[len(prefix):].strip()
                break
        if command:
            break

    # Если не нашли с аргументом — проверяем точное совпадение
    if not command:
        exact_commands = {
            'gc': ['/gc', 'gc', 'код'],
            'tl': ['/tl', 'tl', '/tradelink', 'tradelink'],
            'sid': ['/sid', 'sid', '/steamid', 'steamid'],
            'sl': ['/sl', 'sl', '/steamlink', 'steamlink', '/profile', 'profile'],
            'group': ['/group', 'group', '/showgroup', 'showgroup', '/группа', 'группа'],
            'inv': ['/inv', 'inv', '/inventory', 'inventory', '/инвентарь', 'инвентарь'],
        }
        for cmd, cmds in exact_commands.items():
            if query_lower in cmds:
                command = cmd
                argument = ""
                break

    # Если команда не определена — показываем подсказку
    if not command:
        hint = InlineQueryResultArticle(
            id="hint_unknown",
            title="❓ Неизвестная команда",
            description="Доступно: gc, tl, sid, sl, group",
            input_message_content=InputTextMessageContent(
                message_text="❓ <b>Доступные команды:</b>\n\n"
                             "• <code>@steamguardmanager_bot gc</code> — Steam Guard коды\n"
                             "• <code>@steamguardmanager_bot tl</code> — трейд-ссылки\n"
                             "• <code>@steamguardmanager_bot sid</code> — SteamID\n"
                             "• <code>@steamguardmanager_bot sl</code> — ссылка на профиль\n"
                             "• <code>@steamguardmanager_bot group</code> — группы",
                parse_mode="HTML"
            )
        )
        await inline_query.answer([hint], cache_time=10)
        return

    # ========== КОМАНДА БЕЗ АРГУМЕНТА — ПОКАЗЫВАЕМ ПОДСКАЗКУ ==========
    if not argument:
        hints = {
            'gc': (
                "🔐 <b>Steam Guard коды</b>\n\n"
                "Введите <code>@steamguardmanager_bot gc логин</code> для поиска аккаунта\n"
                "Или просто <code>@steamguardmanager_bot gc</code> и пробел чтобы показать все"
            ),
            'tl': (
                "🔗 <b>Трейд-ссылки</b>\n\n"
                "Введите <code>@steamguardmanager_bot tl логин</code> для поиска\n"
                "Ссылка появится когда останется только один вариант"
            ),
            'sid': (
                "🆔 <b>SteamID</b>\n\n"
                "Введите <code>@steamguardmanager_bot sid</code> и пробел для получения списка"
            ),
            'sl': (
                "🔗 <b>Ссылка на профиль Steam</b>\n\n"
                "Введите <code>@steamguardmanager_bot sl логин</code> для поиска\n"
                "Ссылка появится когда останется только один вариант"
            ),
            'group': (
                "📂 <b>Группы аккаунтов</b>\n\n"
                "Введите <code>@steamguardmanager_bot group название</code> для поиска группы\n"
                "Или просто <code>@steamguardmanager_bot group</code> и пробел чтобы показать все"
            ),
            'inv': (
                "🎒 <b>Инвентарь</b>\n\n"
                "Введите <code>@steamguardmanager_bot inv [игра] логин</code>\n\n"
                "Игры: <code>cs</code>, <code>tf</code>, <code>dota</code>, <code>steam</code>\n"
                "Пример: <code>@steamguardmanager_bot inv cs mylogin</code>"
            ),
        }

        hint_text = hints.get(command, "Введите аргумент для поиска")

        hint_result = InlineQueryResultArticle(
            id=f"hint_{command}",
            title=f"ℹ️ Введите аргумент после {command}",
            description="Добавьте пробел и начните вводить для поиска",
            input_message_content=InputTextMessageContent(
                message_text=hint_text,
                parse_mode="HTML"
            ),
        )
        await inline_query.answer([hint_result], cache_time=10)
        return

    # ========== КОМАНДА С АРГУМЕНТОМ — ВЫПОЛНЯЕМ ==========
    if command == 'gc':
        await inline_gc(inline_query, user_id)
    elif command == 'tl':
        await inline_tradelink(inline_query, user_id)
    elif command == 'sid':
        await inline_steamid(inline_query, user_id)
    elif command == 'sl':
        await inline_steamlink(inline_query, user_id)
    elif command == 'group':
        await inline_showgroup(inline_query, user_id)
    elif command == 'inv':
        await inline_inv(inline_query, user_id)

async def inline_gc(inline_query: InlineQuery, user_id: int):
    """Инлайн: получение Steam Guard кодов"""
    query = inline_query.query.strip()
    query_lower = query.lower()

    prefixes = ['/gc', 'gc', 'код', '/getcode', 'getcode', '/guardcode', 'guardcode']
    for prefix in prefixes:
        if query_lower.startswith(prefix):
            query = query[len(prefix):].strip()
            break

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(
            Mafile.telegram_id == user_id,
            Mafile.shared_secret.isnot(None)
        )

        if query:
            stmt = stmt.where(Mafile.account_name.ilike(f"%{query}%"))

        stmt = stmt.order_by(
            Mafile.is_pinned.desc(),
            Mafile.sort_order.asc()
        ).limit(50)

        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        # 🔥 Загружаем все группы пользователя за один запрос
        groups_stmt = select(AccountGroup).where(AccountGroup.telegram_id == user_id)
        groups_result = await session.execute(groups_stmt)
        groups = {g.id: g.name for g in groups_result.scalars().all()}

    if not mafiles:
        no_result = InlineQueryResultArticle(
            id="no_gc",
            title="❌ Нет аккаунтов с Steam Guard",
            description="Добавьте аккаунты через бота или проверьте логин",
            input_message_content=InputTextMessageContent(
                message_text="❌ Аккаунты с Steam Guard не найдены\n"
                             "Добавьте аккаунт в боте: @bot_name",
                parse_mode="HTML"
            )
        )
        await inline_query.answer([no_result], cache_time=1)
        return

    results = []
    local_time = int(time.time())

    for mafile in mafiles:
        shared_secret = decrypt_value(mafile.shared_secret) if mafile.shared_secret else ""
        if not shared_secret:
            continue

        time_buffer = struct.pack('>Q', local_time // 30)
        time_hmac = hmac.new(base64.b64decode(shared_secret), time_buffer, digestmod=hashlib.sha1).digest()
        begin = time_hmac[19] & 0xF
        full_code = struct.unpack('>I', time_hmac[begin:begin + 4])[0] & 0x7FFFFFFF
        chars = '23456789BCDFGHJKMNPQRTVWXY'
        code = ''
        temp = full_code
        for _ in range(5):
            temp, i = divmod(temp, len(chars))
            code += chars[i]

        remaining_seconds = 30 - (local_time % 30)

        # 🔥 Используем словарь groups вместо mafile.group.name
        group_name = groups.get(mafile.group_id) if mafile.group_id else None
        group_text = f" 📁{group_name}" if group_name else ""

        title = f"{'📌 ' if mafile.is_pinned else ''}{mafile.account_name}{group_text}"
        description = f"Код: {code} (обновится через {remaining_seconds}с)"

        message_text = (
            f"🔐 <b>STEAM GUARD КОД</b>\n\n"
            f"📱 <b>Аккаунт:</b> <code>{mafile.account_name}</code>\n"
            f"🔑 <b>Код:</b> <code>{code}</code>\n\n"
            f"⏰ Обновится через <b>{remaining_seconds}с</b>"
        )

        results.append(
            InlineQueryResultArticle(
                id=f"gc_{mafile.id}",
                title=title,
                description=description,
                input_message_content=InputTextMessageContent(
                    message_text=message_text,
                    parse_mode="HTML"
                ),
            )
        )

    cache_seconds = max(1, remaining_seconds - 3)
    await inline_query.answer(results, cache_time=cache_seconds)

async def inline_tradelink(inline_query: InlineQuery, user_id: int):
    """Инлайн: получение трейд-ссылок"""
    query = inline_query.query.strip()
    query_lower = query.lower()

    prefixes = ['/tl', 'tl', '/tradelink', 'tradelink']
    for prefix in prefixes:
        if query_lower.startswith(prefix + ' '):
            query = query[len(prefix):].strip()
            break

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(
            Mafile.telegram_id == user_id,
            Mafile.access_token.isnot(None)
        )

        if query:
            stmt = stmt.where(Mafile.account_name.ilike(f"%{query}%"))

        stmt = stmt.order_by(
            Mafile.is_pinned.desc(),
            Mafile.sort_order.asc()
        ).limit(50)

        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        groups_stmt = select(AccountGroup).where(AccountGroup.telegram_id == user_id)
        groups_result = await session.execute(groups_stmt)
        groups = {g.id: g.name for g in groups_result.scalars().all()}

    if not mafiles:
        no_result = InlineQueryResultArticle(
            id="no_tl",
            title="❌ Нет аккаунтов с активной сессией",
            description="Сначала войдите в аккаунты через бота",
            input_message_content=InputTextMessageContent(
                message_text="❌ Нет аккаунтов с активной сессией\n"
                             "Войдите в аккаунт через бота: @steamguardmanager_bot",
                parse_mode="HTML"
            )
        )
        await inline_query.answer([no_result], cache_time=10)
        return

    # 🔥 Если найден ровно ОДИН аккаунт — сразу получаем трейд-ссылку
    if len(mafiles) == 1:
        mafile = mafiles[0]
        client = await SteamSessionManager.create_steam_client_from_db(mafile, session)
        try:
            tradelink = await client.get_tradelink()
        except:
            tradelink = None
        finally:
            await client.close()

        group_name = groups.get(mafile.group_id) if mafile.group_id else None
        group_text = f" 📁{group_name}" if group_name else ""

        if tradelink:
            message_text = (
                f"🔗 <b>Трейд-ссылка аккаунта {mafile.account_name}</b> — {tradelink}"
            )
            description = tradelink[:60]
        else:
            message_text = (
                f"⚠️ <b>Не удалось получить трейд-ссылку для {mafile.account_name}</b>\n"
                f"Возможно, сессия устарела. Войдите заново."
            )
            description = "Сессия устарела"

        single_result = InlineQueryResultArticle(
            id=f"tl_single_{mafile.id}",
            title=f"🔗 {mafile.account_name}{group_text}",
            description=description,
            input_message_content=InputTextMessageContent(
                message_text=message_text,
                parse_mode="HTML"
            ),
        )
        await inline_query.answer([single_result], cache_time=30)
        return

    # 🔥 Много аккаунтов — показываем список, ссылка получится при нажатии
    # 🔥 Много аккаунтов — показываем список, ссылка появится когда останется один
    results = []

    for mafile in mafiles:
        group_name = groups.get(mafile.group_id) if mafile.group_id else None
        group_text = f" 📁{group_name}" if group_name else ""

        title = f"🔗 {mafile.account_name}{group_text}"

        # 🔥 Понятное описание
        if len(mafiles) == 1:
            description = "Загружаю ссылку..."
        elif len(mafiles) <= 5:
            description = f"Введите логин полностью ({len(mafiles)} похожих)"
        else:
            description = f"Уточните запрос — найдено {len(mafiles)} аккаунтов"

        results.append(
            InlineQueryResultArticle(
                id=f"tl_{mafile.id}",
                title=title,
                description=description,
                input_message_content=InputTextMessageContent(
                    message_text=f"🔗 Загружаю трейд-ссылку для <b>{mafile.account_name}</b>...",
                    parse_mode="HTML"
                ),
            )
        )

    await inline_query.answer(results, cache_time=5)

async def inline_steamid(inline_query: InlineQuery, user_id: int):
    """Инлайн: получение SteamID"""
    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(
            Mafile.telegram_id == user_id,
            Mafile.steamid.isnot(None)
        ).order_by(
            Mafile.is_pinned.desc(),
            Mafile.sort_order.asc()
        ).limit(50)

        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        # 🔥 Загружаем все группы
        groups_stmt = select(AccountGroup).where(AccountGroup.telegram_id == user_id)
        groups_result = await session.execute(groups_stmt)
        groups = {g.id: g.name for g in groups_result.scalars().all()}

    if not mafiles:
        no_result = InlineQueryResultArticle(
            id="no_sid",
            title="❌ Нет аккаунтов с SteamID",
            description="Сначала войдите в аккаунты",
            input_message_content=InputTextMessageContent(
                message_text="❌ Нет аккаунтов с привязанным SteamID",
                parse_mode="HTML"
            )
        )
        await inline_query.answer([no_result], cache_time=10)
        return

    results = []

    for mafile in mafiles:
        steamid64 = str(mafile.steamid)

        try:
            sid64 = int(steamid64)
            account_id = sid64 & 0xFFFFFFFF
            steamid2 = f"STEAM_0:{account_id & 1}:{account_id >> 1}"
        except:
            steamid2 = steamid64

        group_name = groups.get(mafile.group_id) if mafile.group_id else None
        group_text = f" 📁{group_name}" if group_name else ""

        message_text = (
            f"🆔 <b>Стим ID для аккаунта {mafile.account_name}</b>\n"
            f"├─ SteamID: <code>{steamid2}</code>\n"
            f"├─ SteamID64: <code>{steamid64}</code>\n"
            f"└─ <a href='https://steamcommunity.com/profiles/{steamid64}'>Профиль Steam</a>"
        )

        results.append(
            InlineQueryResultArticle(
                id=f"sid_{mafile.id}",
                title=f"🆔 {mafile.account_name}{group_text}",
                description=f"SteamID: {steamid2}",
                input_message_content=InputTextMessageContent(
                    message_text=message_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True
                ),
            )
        )

    await inline_query.answer(results, cache_time=300)

async def inline_showgroup(inline_query: InlineQuery, user_id: int):
    """Инлайн: просмотр групп"""
    query = inline_query.query.strip()
    query_lower = query.lower()

    # Убираем префиксы
    prefixes = ['/showgroup', 'showgroup', '/group', 'group', '/groups', 'groups', '/группа', 'группа']
    for prefix in prefixes:
        if query_lower.startswith(prefix + ' '):
            query = query[len(prefix):].strip()
            break

    async with AsyncSessionLocal() as session:
        stmt = select(AccountGroup).where(AccountGroup.telegram_id == user_id)

        if query:
            stmt = stmt.where(AccountGroup.name.ilike(f"%{query}%"))

        stmt = stmt.order_by(AccountGroup.sort_order)
        result = await session.execute(stmt)
        groups = result.scalars().all()

        if not groups:
            no_result = InlineQueryResultArticle(
                id="no_group",
                title="❌ Группы не найдены",
                description="Создайте группы в боте: /groups" + (f" (поиск: {query})" if query else ""),
                input_message_content=InputTextMessageContent(
                    message_text="❌ Группы не найдены\nСоздайте группы в боте: @steamguardmanager_bot",
                    parse_mode="HTML"
                )
            )
            await inline_query.answer([no_result], cache_time=10)
            return

        results = []

        for grp in groups:
            stmt = select(func.count(Mafile.id)).where(Mafile.group_id == grp.id)
            result = await session.execute(stmt)
            count = result.scalar() or 0

            if count == 0:
                continue

            stmt = select(Mafile).where(Mafile.group_id == grp.id).order_by(
                Mafile.is_pinned.desc(),
                Mafile.sort_order.asc()
            )
            result = await session.execute(stmt)
            mafiles = result.scalars().all()

            accounts_list = ""
            for idx, mf in enumerate(mafiles[:20], 1):
                pin = "📌" if mf.is_pinned else ""
                guard = "🔐" if mf.shared_secret else "❌"
                has_login = "✅" if (mf.access_token and mf.refresh_token) else "⚠️"
                accounts_list += f"{idx}. {pin}{has_login}{guard} <code>{mf.account_name}</code>\n"

            if len(mafiles) > 20:
                accounts_list += f"<i>... и еще {len(mafiles) - 20}</i>\n"

            message_text = (
                f"📁 <b>Список аккаунтов из группы {grp.name}</b> ({count} акк.)\n\n"
                f"{accounts_list}"
            )

            results.append(
                InlineQueryResultArticle(
                    id=f"group_{grp.id}",
                    title=f"📁 {grp.name}",
                    description=f"Аккаунтов: {count}",
                    input_message_content=InputTextMessageContent(
                        message_text=message_text,
                        parse_mode="HTML"
                    ),
                )
            )

    # 🔥 Состав групп меняется редко — кэш 10 секунд
    await inline_query.answer(results, cache_time=300)

async def inline_steamlink(inline_query: InlineQuery, user_id: int):
    """Инлайн: получение ссылки на профиль Steam"""
    query = inline_query.query.strip()
    query_lower = query.lower()

    # Убираем префиксы
    prefixes = ['/sl', 'sl', '/steamlink', 'steamlink', '/profile', 'profile']
    for prefix in prefixes:
        if query_lower.startswith(prefix + ' '):
            query = query[len(prefix):].strip()
            break

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(
            Mafile.telegram_id == user_id,
            Mafile.steamid.isnot(None)
        )

        if query:
            stmt = stmt.where(Mafile.account_name.ilike(f"%{query}%"))

        stmt = stmt.order_by(
            Mafile.is_pinned.desc(),
            Mafile.sort_order.asc()
        ).limit(50)

        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        groups_stmt = select(AccountGroup).where(AccountGroup.telegram_id == user_id)
        groups_result = await session.execute(groups_stmt)
        groups = {g.id: g.name for g in groups_result.scalars().all()}

    if not mafiles:
        no_result = InlineQueryResultArticle(
            id="no_sl",
            title="❌ Нет аккаунтов с SteamID",
            description="Сначала войдите в аккаунты",
            input_message_content=InputTextMessageContent(
                message_text="❌ Нет аккаунтов с привязанным SteamID\n"
                             "Войдите в аккаунт через бота: @steamguardmanager_bot",
                parse_mode="HTML"
            )
        )
        await inline_query.answer([no_result], cache_time=10)
        return

    # 🔥 Если найден ровно ОДИН аккаунт — сразу показываем ссылку
    if len(mafiles) == 1:
        mafile = mafiles[0]
        steamid64 = str(mafile.steamid)
        profile_url = f"https://steamcommunity.com/profiles/{steamid64}"

        group_name = groups.get(mafile.group_id) if mafile.group_id else None
        group_text = f" 📁{group_name}" if group_name else ""

        message_text = (
            f"🔗 <b>Ссылка на Steam профиль аккаунта {mafile.account_name}</b> — {profile_url}"
        )

        single_result = InlineQueryResultArticle(
            id=f"sl_single_{mafile.id}",
            title=f"🔗 {mafile.account_name}{group_text}",
            description=profile_url,
            input_message_content=InputTextMessageContent(
                message_text=message_text,
                parse_mode="HTML",
                disable_web_page_preview=False
            ),
        )
        await inline_query.answer([single_result], cache_time=300)
        return

    # 🔥 Много аккаунтов — показываем список
    results = []

    for mafile in mafiles:
        group_name = groups.get(mafile.group_id) if mafile.group_id else None
        group_text = f" 📁{group_name}" if group_name else ""

        title = f"🔗 {mafile.account_name}{group_text}"

        if len(mafiles) <= 5:
            description = f"Введите логин полностью ({len(mafiles)} похожих)"
        else:
            description = f"Уточните запрос — найдено {len(mafiles)} аккаунтов"

        steamid64 = str(mafile.steamid)
        profile_url = f"https://steamcommunity.com/profiles/{steamid64}"

        results.append(
            InlineQueryResultArticle(
                id=f"sl_{mafile.id}",
                title=title,
                description=description,
                input_message_content=InputTextMessageContent(
                    message_text=f"🔗 <b>Ссылка на Steam профиль аккаунта {mafile.account_name}</b> — {profile_url}",
                    parse_mode="HTML",
                    disable_web_page_preview=True
                ),
            )
        )

    await inline_query.answer(results, cache_time=300)

async def inline_inv(inline_query: InlineQuery, user_id: int):
    """Инлайн: просмотр инвентаря"""
    query = inline_query.query.strip()
    query_lower = query.lower()

    # Убираем префикс inv если есть
    inv_prefixes = ['/inv ', 'inv ', '/inventory ', 'inventory ', '/инвентарь ', 'инвентарь ']
    for prefix in inv_prefixes:
        if query_lower.startswith(prefix):
            query = query[len(prefix):].strip()
            break

    # Определяем игру и логин
    game_map = {
        'cs': 730, 'cs2': 730, 'csgo': 730,
        'tf': 440, 'tf2': 440,
        'dota': 570, 'dota2': 570,
        'steam': 753,
    }

    parts = query.split()
    app_id = 730
    account_query = ""

    if parts:
        if parts[0].lower() in game_map:
            app_id = game_map[parts[0].lower()]
            account_query = ' '.join(parts[1:])
        else:
            account_query = ' '.join(parts)

    context_id = 2 if app_id != 753 else 6
    game_names = {730: "CS2", 440: "TF2", 570: "Dota 2", 753: "Steam"}
    game_name = game_names.get(app_id, "CS2")

    async with AsyncSessionLocal() as session:
        stmt = select(Mafile).where(
            Mafile.telegram_id == user_id,
            Mafile.steamid.isnot(None)
        )
        if account_query:
            stmt = stmt.where(Mafile.account_name.ilike(f"%{account_query}%"))
        stmt = stmt.order_by(Mafile.is_pinned.desc(), Mafile.sort_order.asc()).limit(50)
        result = await session.execute(stmt)
        mafiles = result.scalars().all()

        groups_stmt = select(AccountGroup).where(AccountGroup.telegram_id == user_id)
        groups_result = await session.execute(groups_stmt)
        groups = {g.id: g.name for g in groups_result.scalars().all()}

    if not mafiles:
        await inline_query.answer([InlineQueryResultArticle(
            id="no_inv",
            title="❌ Нет аккаунтов с привязанным SteamID",
            description="Сначала войдите в аккаунты",
            input_message_content=InputTextMessageContent(
                message_text=f"❌ Нет аккаунтов с привязанным SteamID\n\nФормат: @steamguardmanager_bot inv [cs/tf/dota/steam] логин",
                parse_mode="HTML"
            )
        )], cache_time=10)
        return

    if len(mafiles) != 1:
        results = []
        for mafile in mafiles:
            group_name = groups.get(mafile.group_id)
            group_text = f" 📁{group_name}" if group_name else ""
            has_token = "✅" if mafile.access_token else "⚠️"
            desc = f"Требуется вход | {game_name}" if not mafile.access_token else f"Введите логин полностью | {game_name}"
            results.append(InlineQueryResultArticle(
                id=f"inv_list_{mafile.id}",
                title=f"🎒 {has_token} {mafile.account_name}{group_text}",
                description=desc,
                input_message_content=InputTextMessageContent(
                    message_text=f"🎒 Уточните запрос для просмотра инвентаря {game_name}",
                    parse_mode="HTML"
                )
            ))
        await inline_query.answer(results, cache_time=5)
        return

    mafile = mafiles[0]

    if not mafile.access_token:
        await inline_query.answer([InlineQueryResultArticle(
            id=f"inv_notoken_{mafile.id}",
            title=f"⚠️ Требуется вход в аккаунт {mafile.account_name}",
            description="Сессия не активна, выполните /login",
            input_message_content=InputTextMessageContent(
                message_text=f"⚠️ Для просмотра инвентаря <b>{mafile.account_name}</b> требуется вход.",
                parse_mode="HTML"
            )
        )], cache_time=10)
        return

    client = await SteamSessionManager.create_steam_client_from_db(mafile, session)
    try:
        data = await client.get_inventory(app_id, context_id)

        if not data:
            await inline_query.answer([InlineQueryResultArticle(
                id=f"inv_error_{mafile.id}",
                title=f"❌ Не удалось загрузить инвентарь",
                description="Профиль скрыт или сессия устарела",
                input_message_content=InputTextMessageContent(
                    message_text=f"❌ Не удалось загрузить инвентарь для <b>{mafile.account_name}</b>",
                    parse_mode="HTML"
                )
            )], cache_time=10)
            return

        items = process_inventory_items_universal(data, app_id)
        total_count = data.get('total_inventory_count', len(items))

        # Сортировка: скины → кейсы → чармы → всё остальное
        rarity_order = {'Covert': 0, 'Extraordinary': 0, 'Classified': 1, 'Restricted': 2, 'Mil-Spec Grade': 3, 'Industrial Grade': 4, 'Consumer Grade': 5, 'Base Grade': 6}

        def sort_category(item):
            name = item.get('name', '').lower()
            if item.get('exterior') or item.get('float') is not None or item.get('is_stattrak'):
                return 0
            if 'case' in name or 'capsule' in name or 'package' in name:
                return 1
            if 'charm' in name:
                return 2
            return 3

        items.sort(key=lambda x: (
            sort_category(x),
            rarity_order.get(x.get('rarity', ''), 99),
            x.get('name', '')
        ))

        if not items:
            await inline_query.answer([InlineQueryResultArticle(
                id=f"inv_empty_{mafile.id}",
                title=f"🎒 Инвентарь {game_name}: {mafile.account_name}",
                description="Инвентарь пуст",
                input_message_content=InputTextMessageContent(
                    message_text=f"🎒 Инвентарь {game_name} — {mafile.account_name}\n📭 Инвентарь пуст",
                    parse_mode="HTML"
                )
            )], cache_time=10)
            return

        # Разбиваем на страницы по 20 предметов
        ITEMS_PER_PAGE = 30
        total_pages = (len(items) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE

        results = []

        for page in range(total_pages):
            start = page * ITEMS_PER_PAGE
            end = start + ITEMS_PER_PAGE
            page_items = items[start:end]

            if total_pages == 1:
                header = f"🎒 <b>Инвентарь {game_name}</b> — <b>{mafile.account_name}</b>\n📦 {total_count} предметов\n"
            else:
                header = f"🎒 <b>Инвентарь {game_name}</b> — <b>{mafile.account_name}</b>\n📦 {total_count} предметов | Стр. {page+1}/{total_pages}\n\n"

            lines = []

            for idx, item in enumerate(page_items, start + 1):
                rarity_emoji = item.get('rarity_emoji', '⚪')
                name = item.get('name', 'Unknown')
                amount = f" x{item['amount']}" if item.get('amount', 1) > 1 else ""

                line = f"{idx}. {rarity_emoji} <b>{name}</b>{amount}"

                info_parts = []

                ext = item.get('exterior', '')
                if ext and ext not in name:
                    info_parts.append(ext)

                if item.get('is_stattrak'):
                    st = "StatTrak™"
                    if item.get('stattrak_count'):
                        st += f" ({item['stattrak_count']})"
                    info_parts.append(st)

                tech = []
                if item.get('float') is not None:
                    tech.append(f"Float: {item['float']:.6f}")
                if item.get('pattern') is not None:
                    tech.append(f"Pattern: {item['pattern']}")
                if item.get('paint_seed') is not None:
                    tech.append(f"Seed: {item['paint_seed']}")
                if tech:
                    info_parts.append(', '.join(tech))

                if item.get('hero'):
                    info_parts.append(item['hero'])
                if item.get('quality'):
                    info_parts.append(item['quality'])

                if info_parts:
                    line += f"\n   {' | '.join(info_parts)}"

                stickers = item.get('stickers', [])
                if stickers:
                    for s in stickers:
                        w = f" ({math.ceil(s['wear']*100)}%)" if s.get('wear') and s['wear'] > 0 else ""
                        line += f"\n   🏷️ {s['name']}{w}"

                lines.append(line)

            text = header + "\n\n".join(lines)

            if total_pages == 1:
                desc = f"Предметов: {total_count}"
            else:
                first_item = page_items[0].get('name', '')[:30]
                last_item = page_items[-1].get('name', '')[:30]
                desc = f"Стр. {page+1}/{total_pages} | {first_item} ... {last_item}"

            results.append(InlineQueryResultArticle(
                id=f"inv_{mafile.id}_p{page}",
                title=f"🎒 {game_name}: {mafile.account_name} (стр. {page+1}/{total_pages})",
                description=desc,
                input_message_content=InputTextMessageContent(
                    message_text=text,
                    parse_mode="HTML"
                )
            ))

        await inline_query.answer(results, cache_time=10)

    except Exception as e:
        logger.error(f"INV error: {e}")
        await inline_query.answer([InlineQueryResultArticle(
            id=f"inv_exc_{mafile.id}",
            title=f"❌ Ошибка: {str(e)[:60]}",
            description="Попробуйте позже",
            input_message_content=InputTextMessageContent(
                message_text=f"❌ Ошибка: {str(e)[:200]}",
                parse_mode="HTML"
            )
        )], cache_time=10)
    finally:
        await client.close()
