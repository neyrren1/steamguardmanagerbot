"""Handlers are registered on routers composed in one explicit order."""

import ast
from pathlib import Path

import pytest
from aiogram import Dispatcher

from guardbot.bot import routing, runtime

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "guardbot" / "bot" / "handlers"
SUPPORT_MODULES = {"callback_builders"}


def _handler_modules() -> list[str]:
    return sorted(
        p.stem
        for p in HANDLERS.glob("*.py")
        if p.stem != "__init__" and p.stem not in SUPPORT_MODULES
    )


def test_every_handler_module_has_one_router_in_the_composition() -> None:
    import importlib

    routers = [
        importlib.import_module(f"guardbot.bot.handlers.{name}").router
        for name in _handler_modules()
    ]
    assert sorted(id(r) for r in routers) == sorted(id(r) for r in routing.ROUTERS)
    assert len(set(map(id, routing.ROUTERS))) == len(routing.ROUTERS)


def test_support_modules_do_not_register_handlers() -> None:
    import importlib

    for name in SUPPORT_MODULES:
        module = importlib.import_module(f"guardbot.bot.handlers.{name}")
        assert not hasattr(module, "router")


@pytest.mark.parametrize("name", _handler_modules())
def test_handler_modules_never_register_on_the_dispatcher(name) -> None:
    tree = ast.parse((HANDLERS / f"{name}.py").read_text(encoding="utf-8"))
    uses_dp = sorted(
        node.lineno for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == "dp"
    )
    imports_dp = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and any(alias.name == "dp" for alias in node.names)
    ]
    assert (uses_dp, imports_dp) == ([], [])


def test_unfiltered_fallback_router_is_last_and_holds_the_last_message_handler() -> None:
    from guardbot.bot.handlers import messages

    assert routing.ROUTERS[-1] is messages.router
    assert [h.callback for h in messages.router.message.handlers] == [messages.handle_messages]
    assert messages.router.message.handlers[0].filters in (None, [])


def test_routers_are_attached_to_the_runtime_dispatcher_in_order() -> None:
    assert tuple(runtime.dp.sub_routers) == routing.ROUTERS


def test_setup_routers_is_idempotent_and_refuses_a_second_dispatcher() -> None:
    routing.setup_routers(runtime.dp)
    assert tuple(runtime.dp.sub_routers) == routing.ROUTERS

    with pytest.raises(RuntimeError, match="another dispatcher"):
        routing.setup_routers(Dispatcher())
    assert tuple(runtime.dp.sub_routers) == routing.ROUTERS


def test_dispatcher_level_handlers_are_only_the_runtime_ones() -> None:
    names = {
        event: [h.callback.__name__ for h in runtime.dp.observers[event].handlers]
        for event in ("message", "callback_query", "inline_query")
    }
    assert names == {
        "message": ["process_custom_price"],
        "callback_query": ["callback_sell_item", "process_back_to_start"],
        "inline_query": [],
    }
