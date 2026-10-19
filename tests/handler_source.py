"""Locate handler source by code identity instead of by file name.

Security tests inspect handler implementations. Handlers move between modules
during refactoring, so a scan pinned to one file name either breaks loudly or,
worse, keeps passing vacuously after the code has moved away. These helpers
search the whole handlers package and require exactly one definition.
"""

import ast
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "guardbot"
HANDLERS = PACKAGE / "bot" / "handlers"


def handler_paths() -> list[Path]:
    paths = sorted(HANDLERS.glob("*.py"))
    assert paths, "handlers package must not be empty"
    return paths


def package_paths() -> list[Path]:
    paths = sorted(PACKAGE.rglob("*.py"))
    assert any(path.parent == HANDLERS for path in paths)
    return paths


@cache
def _parsed(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def handler_function(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Return the single top-level handler-package definition named ``name``."""
    matches = [
        (path, node)
        for path in handler_paths()
        for node in _parsed(path).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    assert len(matches) == 1, (name, [str(path.relative_to(ROOT)) for path, _ in matches])
    return matches[0][1]
