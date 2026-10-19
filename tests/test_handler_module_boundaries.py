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
