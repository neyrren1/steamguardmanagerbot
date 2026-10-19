"""Extracted from the legacy bot module without behavior changes."""

from html import escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from guardbot.bot.callback_validation import validated_callback_data
from guardbot.bot.runtime import (
    bot,
    callback_feedback,
    check_proxy_available,
    close_owned_resource,
    clear_user_workflows,
    show_recovery_status,
)
from guardbot.config import logger
from guardbot.database import AsyncSessionLocal, Mafile
from guardbot.services.ownership import get_owned_mafile
from guardbot.services.session_manager import SteamSessionManager
from sqlalchemy import text
import re

router = Router(name=__name__)


from guardbot.bot.handlers.callback_builders import (
    _bounded_identifier,
    _confirmation_callback,
    _inventory_page_callback,
    _item_detail_callback,
    _trade_accept_callback,
    _trade_cancel_callback,
    _trade_decline_callback,
    _trade_detail_callback,
)










from guardbot.bot.handlers.trade_operations import (
    fetch_and_display_trades,
    callback_trade_accept,
    callback_trade_decline,
    callback_trade_cancel_outgoing,
    callback_trades_incoming_active,
    callback_trades_incoming_history,
    callback_trades_outgoing_active,
    callback_trades_outgoing_history,
    callback_trade_detail_full,
    callback_back_to_trades_list,
    fetch_and_redirect_trades,
)  # noqa: F401 - compatibility exports for guardbot.compat and existing imports


from guardbot.bot.handlers.trade_notifications import (
    _start_trade_notification_interval_flow,
    callback_trade_notifications_menu,
    callback_trade_notify_interval,
    callback_trade_notify_toggle,
    callback_trades_menu,
    process_trade_notification_interval,
)  # noqa: F401 - compatibility exports for guardbot.compat and existing imports


from guardbot.bot.handlers.trade_link import callback_tradelink  # noqa: F401 - compatibility export


from guardbot.bot.handlers.confirmations import (
    callback_cancel_all,
    callback_cancel_single,
    callback_confirm_all,
    callback_confirm_single,
    callback_confirmations_menu,
)  # noqa: F401 - compatibility exports for commands and existing imports


from guardbot.bot.handlers.inventory import (
    callback_inventory_app,
    callback_inventory_menu,
    callback_item_detail,
    process_inventory_items,
    process_inventory_items_simple,
)  # noqa: F401 - compatibility exports for guardbot.compat and existing imports


from guardbot.bot.handlers.security_controls import (
    _reject_secret_callback_outside_private_chat,
    _start_bot_password_prompt,
    callback_cancel_password_to_settings,
    callback_change_password,
    callback_lock_bot,
    callback_remove_password,
    callback_set_lock_timeout,
    callback_set_password,
    callback_unlock_bot,
)  # noqa: F401 - compatibility exports for guardbot.compat and existing imports
