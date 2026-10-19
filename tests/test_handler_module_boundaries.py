"""Structural contracts for feature-handler module extraction."""


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

    assert routing.ROUTERS.index(features.router) + 1 == routing.ROUTERS.index(
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
        assert all(
            handler.callback is not callback
            for handler in features.router.callback_query.handlers
        )



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
        features.router
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
        assert all(
            handler.callback is not callback
            for handler in (
                *features.router.message.handlers,
                *features.router.callback_query.handlers,
            )
        )


def test_callback_builders_are_shared_without_duplicate_implementations() -> None:
    import ast
    from pathlib import Path

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

    features_path = Path(features.__file__)
    tree = ast.parse(features_path.read_text(encoding="utf-8"))
    definitions = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert definitions.isdisjoint(names)



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
        features.router
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
        assert all(
            handler.callback is not callback
            for handler in features.router.callback_query.handlers
        )



def test_trade_link_has_a_dedicated_router_and_compatibility_export() -> None:
    import ast
    from pathlib import Path

    from guardbot.bot import routing
    from guardbot.bot.handlers import features, trade_link

    assert routing.ROUTERS.index(trade_link.router) + 1 == routing.ROUTERS.index(
        features.router
    )
    assert routing.ROUTERS.count(trade_link.router) == 1
    assert features.callback_tradelink is trade_link.callback_tradelink
    assert sum(
        handler.callback is trade_link.callback_tradelink
        for handler in trade_link.router.callback_query.handlers
    ) == 1
    assert all(
        handler.callback is not trade_link.callback_tradelink
        for handler in features.router.callback_query.handlers
    )

    features_tree = ast.parse(Path(features.__file__).read_text(encoding="utf-8"))
    definitions = {
        node.name
        for node in features_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "callback_tradelink" not in definitions
