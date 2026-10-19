"""Monkeypatches on handler modules must target names that module's code reads.

``monkeypatch.setattr(module, "name", fake)`` only affects code whose module
globals contain ``name``. When a handler moves to another module, a test that
keeps patching the old module still passes, but its fake is never used: the
test silently stops exercising what it claims to. This guard fails on such
vacuous patches.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "guardbot" / "bot" / "handlers"


def _names_read(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names |= {sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)}
    return names


def _lazy_imports() -> dict[str, set[str]]:
    """Names other modules import from a handler module inside a function."""
    found: dict[str, set[str]] = {}
    for path in (ROOT / "guardbot").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.ImportFrom):
                    continue
                module = sub.module or ""
                if module.startswith("guardbot.bot.handlers."):
                    found.setdefault(module.rsplit(".", 1)[1], set()).update(
                        alias.name for alias in sub.names
                    )
    return found


def test_handler_module_monkeypatches_are_not_vacuous() -> None:
    reads = {
        path.stem: _names_read(ast.parse(path.read_text(encoding="utf-8")))
        for path in HANDLERS.glob("*.py")
    }
    lazy = _lazy_imports()
    checked = 0
    vacuous: list[str] = []

    for path in sorted((ROOT / "tests").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in tree.body:
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            aliases = {
                alias.asname or alias.name: alias.name
                for node in ast.walk(func)
                if isinstance(node, ast.ImportFrom) and node.module == "guardbot.bot.handlers"
                for alias in node.names
            }
            for node in ast.walk(func):
                if not (
                    isinstance(node, ast.Call)
                    and getattr(node.func, "attr", "") == "setattr"
                    and len(node.args) >= 2
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id in aliases
                    and isinstance(node.args[1], ast.Constant)
                ):
                    continue
                module = aliases[node.args[0].id]
                name = node.args[1].value
                if module not in reads:
                    continue
                checked += 1
                if name not in reads[module] and name not in lazy.get(module, set()):
                    vacuous.append(f"{path.name}:{node.lineno} {module}.{name}")

    assert checked > 100
    assert vacuous == []
