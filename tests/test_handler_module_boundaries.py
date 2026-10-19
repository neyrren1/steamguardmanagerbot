"""Structural contracts for feature-handler module extraction."""

import ast
from pathlib import Path


def _top_level_definitions(module) -> set[str]:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }



def _trade_operations_router():
    from guardbot.bot.handlers import trade_operations

    return trade_operations.router


def test_features_is_a_pure_compatibility_facade() -> None:
    from guardbot.bot import routing
    from guardbot.bot.handlers import features

    assert not hasattr(features, "router")
    assert _top_level_definitions(features) == set()
    assert all(
        router.name != "guardbot.bot.handlers.features" for router in routing.ROUTERS
    )


def test_security_controls_have_a_dedicated_router_and_compatibility_exports() -> None:
    from guardbot.bot import routing
    from guardbot.bot.handlers import features, security_controls

    callbacks = (
        "callback_set_password",
        "callback_change_password",
        "callback_remove_password",
        "callback_cancel_password_to_settings",
        "callback_lock_bot",
        "callback_unlock_bot",
        "callback_set_lock_timeout",
    )

    from guardbot.bot.handlers import trade_operations

    assert routing.ROUTERS.index(trade_operations.router) + 1 == routing.ROUTERS.index(
        security_controls.router
    )
    assert routing.ROUTERS.count(security_controls.router) == 1
    for name in callbacks:
        callback = getattr(security_controls, name)
        assert getattr(features, name) is callback
        assert sum(
            handler.callback is callback
            for handler in security_controls.router.callback_query.handlers
        ) == 1
    assert _top_level_definitions(features).isdisjoint(callbacks)



def test_trade_notifications_have_a_dedicated_router_and_compatibility_exports() -> None:
    from guardbot.bot import routing
    from guardbot.bot.handlers import features, trade_notifications

    callbacks = (
        "callback_trades_menu",
        "callback_trade_notifications_menu",
        "callback_trade_notify_toggle",
        "callback_trade_notify_interval",
        "process_trade_notification_interval",
    )

    assert routing.ROUTERS.index(trade_notifications.router) < routing.ROUTERS.index(
        _trade_operations_router()
    )
    assert routing.ROUTERS.count(trade_notifications.router) == 1
    registered = [
        *trade_notifications.router.message.handlers,
        *trade_notifications.router.callback_query.handlers,
    ]
    for name in callbacks:
        callback = getattr(trade_notifications, name)
        assert getattr(features, name) is callback
        assert sum(handler.callback is callback for handler in registered) == 1
    assert _top_level_definitions(features).isdisjoint(callbacks)


def test_callback_builders_are_shared_without_duplicate_implementations() -> None:
    from guardbot.bot.handlers import callback_builders, features

    names = (
        "_bounded_identifier",
        "_item_detail_callback",
        "_confirmation_callback",
        "_trade_accept_callback",
        "_trade_detail_callback",
        "_trade_decline_callback",
        "_trade_cancel_callback",
        "_inventory_page_callback",
    )
    for name in names:
        assert getattr(features, name) is getattr(callback_builders, name)

    assert _top_level_definitions(features).isdisjoint(names)



def test_inventory_has_a_dedicated_router_and_compatibility_exports() -> None:
    from guardbot.bot import routing
    from guardbot.bot.handlers import features, inventory

    callbacks = (
        "callback_inventory_menu",
        "callback_inventory_app",
        "callback_item_detail",
    )
    helpers = ("process_inventory_items_simple", "process_inventory_items")

    assert routing.ROUTERS.index(inventory.router) < routing.ROUTERS.index(
        _trade_operations_router()
    )
    assert routing.ROUTERS.count(inventory.router) == 1
    for name in (*callbacks, *helpers):
        assert getattr(features, name) is getattr(inventory, name)
    for name in callbacks:
        callback = getattr(inventory, name)
        assert sum(
            handler.callback is callback
            for handler in inventory.router.callback_query.handlers
        ) == 1
    assert _top_level_definitions(features).isdisjoint((*callbacks, *helpers))



def test_trade_link_has_a_dedicated_router_and_compatibility_export() -> None:
    from guardbot.bot import routing
    from guardbot.bot.handlers import features, trade_link

    assert routing.ROUTERS.index(trade_link.router) < routing.ROUTERS.index(
        _trade_operations_router()
    )
    assert routing.ROUTERS.count(trade_link.router) == 1
    assert features.callback_tradelink is trade_link.callback_tradelink
    assert sum(
        handler.callback is trade_link.callback_tradelink
        for handler in trade_link.router.callback_query.handlers
    ) == 1

    assert "callback_tradelink" not in _top_level_definitions(features)



def test_confirmations_have_a_dedicated_router_and_compatibility_exports() -> None:
    from guardbot.bot import routing
    from guardbot.bot.handlers import confirmations, features

    callbacks = (
        "callback_confirmations_menu",
        "callback_confirm_single",
        "callback_cancel_single",
        "callback_confirm_all",
        "callback_cancel_all",
    )

    assert routing.ROUTERS.index(confirmations.router) < routing.ROUTERS.index(
        _trade_operations_router()
    )
    assert routing.ROUTERS.count(confirmations.router) == 1
    for name in callbacks:
        callback = getattr(confirmations, name)
        assert getattr(features, name) is callback
        assert sum(
            handler.callback is callback
            for handler in confirmations.router.callback_query.handlers
        ) == 1
    assert _top_level_definitions(features).isdisjoint(callbacks)



def test_trade_operations_have_a_dedicated_router_and_compatibility_exports() -> None:
    from guardbot.bot import routing
    from guardbot.bot.handlers import features, trade_operations

    callbacks = (
        "callback_trade_accept",
        "callback_trade_decline",
        "callback_trade_cancel_outgoing",
        "callback_trades_incoming_active",
        "callback_trades_incoming_history",
        "callback_trades_outgoing_active",
        "callback_trades_outgoing_history",
        "callback_trade_detail_full",
        "callback_back_to_trades_list",
    )
    helpers = ("fetch_and_display_trades", "fetch_and_redirect_trades")

    from guardbot.bot.handlers import confirmations, security_controls

    assert routing.ROUTERS.index(confirmations.router) + 1 == routing.ROUTERS.index(
        trade_operations.router
    )
    assert routing.ROUTERS.index(trade_operations.router) + 1 == routing.ROUTERS.index(
        security_controls.router
    )
    assert routing.ROUTERS.count(trade_operations.router) == 1
    for name in (*callbacks, *helpers):
        assert getattr(features, name) is getattr(trade_operations, name)
    for name in callbacks:
        callback = getattr(trade_operations, name)
        assert sum(
            handler.callback is callback
            for handler in trade_operations.router.callback_query.handlers
        ) == 1
    assert _top_level_definitions(features).isdisjoint((*callbacks, *helpers))
