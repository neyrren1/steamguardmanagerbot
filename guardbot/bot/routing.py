"""Compose the bot's handler routers in one explicit dispatch order.

aiogram hands each update to the first handler whose filters match, so the
order below is part of the bot's behaviour. It is defined here once, instead of
depending on the order in which a launcher happens to import modules.

Handlers declared directly on the dispatcher in ``runtime`` are checked before
every router.
"""

from aiogram import Dispatcher, Router
from aiogram.dispatcher.event.handler import HandlerObject

from guardbot.bot.handlers import (
    account_list,
    account_login,
    account_mafile,
    account_proxy,
    account_settings,
    accounts,
    bulk_import,
    commands,
    common,
    confirmations,
    groups,
    guard_codes,
    imports,
    inline,
    inventory,
    menus,
    messages,
    rich_demo,
    security_controls,
    steam_challenge,
    trade_link,
    trade_notifications,
    trade_operations,
)

# ``messages`` holds the unfiltered fallback handler and must stay last.
ROUTERS: tuple[Router, ...] = (
    menus.router,
    account_list.router,
    account_settings.router,
    account_proxy.router,
    guard_codes.router,
    account_login.router,
    steam_challenge.router,
    account_mafile.router,
    accounts.router,
    common.router,
    commands.router,
    bulk_import.router,
    trade_notifications.router,
    inventory.router,
    trade_link.router,
    confirmations.router,
    trade_operations.router,
    security_controls.router,
    groups.router,
    imports.router,
    inline.router,
    rich_demo.router,
    messages.router,
)


def setup_routers(dispatcher: Dispatcher) -> None:
    """Attach every handler router to ``dispatcher`` exactly once.

    Calling it again for the same dispatcher is a no-op; attaching the routers
    to a second dispatcher is refused, because aiogram routers have one parent.
    """
    parents = [router.parent_router for router in ROUTERS]
    if all(parent is dispatcher for parent in parents):
        return
    if any(parent is not None for parent in parents):
        raise RuntimeError("handler routers are already attached to another dispatcher")
    dispatcher.include_routers(*ROUTERS)


def ordered_handlers(router: Router, event_name: str) -> list[HandlerObject]:
    """Return one event type's handlers in the order aiogram tries them.

    A router checks its own handlers first, then each nested router in the
    order it was included.
    """
    handlers = list(router.observers[event_name].handlers)
    for sub_router in router.sub_routers:
        handlers.extend(ordered_handlers(sub_router, event_name))
    return handlers
