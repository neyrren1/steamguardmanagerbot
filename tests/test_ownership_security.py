import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
HANDLERS = ROOT / "guardbot/bot/handlers"
USER_ENTRYPOINTS = [*HANDLERS.glob("*.py"), ROOT / "guardbot/bot/runtime.py"]


def test_owned_entity_queries_include_telegram_owner() -> None:
    from guardbot.services.ownership import get_owned_group, get_owned_mafile

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class Session:
        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            return Result(SimpleNamespace(id=1, telegram_id=7))

    session = Session()
    asyncio.run(get_owned_mafile(session, 1, 7))
    asyncio.run(get_owned_group(session, 2, 7))

    mafile_sql, group_sql = [str(statement) for statement in session.statements]
    assert "mafiles.id" in mafile_sql
    assert "mafiles.telegram_id" in mafile_sql
    assert "account_groups.id" in group_sql
    assert "account_groups.telegram_id" in group_sql


def test_handlers_do_not_fetch_user_owned_rows_by_primary_key_only() -> None:
    offenders: list[str] = []

    for path in USER_ENTRYPOINTS:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id in {"Mafile", "AccountGroup"}
            ):
                parent = parents.get(node)
                while parent is not None and not isinstance(
                    parent, (ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    parent = parents.get(parent)
                if (
                    path.name == "runtime.py"
                    and parent is not None
                    and parent.name == "check_new_trades_for_account"
                ):
                    continue
                offenders.append(f"{path.name}:{node.lineno}:session.get")

            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "where"
                and isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Name)
                and node.func.value.func.id == "select"
                and node.func.value.args
                and isinstance(node.func.value.args[0], ast.Name)
                and node.func.value.args[0].id in {"Mafile", "AccountGroup"}
            ):
                expression = " ".join(ast.unparse(arg) for arg in node.args)
                model = node.func.value.args[0].id
                if (
                    f"{model}.id" in expression
                    and f"{model}.telegram_id" not in expression
                ):
                    offenders.append(f"{path.name}:{node.lineno}:ownerless select")

    assert offenders == []


def test_trade_redirect_helper_uses_its_callback_parameter() -> None:
    path = HANDLERS / "features.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "fetch_and_redirect_trades"
    )
    loaded_names = {
        node.id
        for node in ast.walk(helper)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }

    assert "callback" not in loaded_names
