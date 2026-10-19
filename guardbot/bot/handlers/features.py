"""Compatibility exports for handlers formerly defined in this module.

Every handler now lives in a dedicated module with its own router; this module
registers nothing. It only keeps historical ``features.<name>`` access and the
``guardbot.compat``/``tgbot`` namespace working.
"""

# ruff: noqa: F401 - every import below is a compatibility re-export.

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
from guardbot.bot.handlers.confirmations import (
    callback_cancel_all,
    callback_cancel_single,
    callback_confirm_all,
    callback_confirm_single,
    callback_confirmations_menu,
)
from guardbot.bot.handlers.inventory import (
    callback_inventory_app,
    callback_inventory_menu,
    callback_item_detail,
    process_inventory_items,
    process_inventory_items_simple,
)
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
)
from guardbot.bot.handlers.trade_link import callback_tradelink
from guardbot.bot.handlers.trade_notifications import (
    _start_trade_notification_interval_flow,
    callback_trade_notifications_menu,
    callback_trade_notify_interval,
    callback_trade_notify_toggle,
    callback_trades_menu,
    process_trade_notification_interval,
)
from guardbot.bot.handlers.trade_operations import (
    callback_back_to_trades_list,
    callback_trade_accept,
    callback_trade_cancel_outgoing,
    callback_trade_decline,
    callback_trade_detail_full,
    callback_trades_incoming_active,
    callback_trades_incoming_history,
    callback_trades_outgoing_active,
    callback_trades_outgoing_history,
    fetch_and_display_trades,
    fetch_and_redirect_trades,
)
