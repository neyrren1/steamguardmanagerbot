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

    assert routing.ROUTERS.index(trade_notifications.router) + 1 == routing.ROUTERS.index(
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
