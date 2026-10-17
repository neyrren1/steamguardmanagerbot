import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace


def test_inline_answers_are_personal_and_not_cached() -> None:
    source_path = Path("guardbot/bot/handlers/inline.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    direct_answers = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "answer":
            continue
        owner = node.func.value
        if isinstance(owner, ast.Name) and owner.id == "inline_query":
            direct_answers.append(node)

    assert direct_answers
    for call in direct_answers:
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        assert ast.literal_eval(keywords["is_personal"]) is True, call.lineno
        assert ast.literal_eval(keywords["cache_time"]) == 0, call.lineno


def test_inline_client_creation_stays_inside_database_session_context() -> None:
    source_path = Path("guardbot/bot/handlers/inline.py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_steam_client_from_db"
    ]
    assert calls

    for call in calls:
        current: ast.AST | None = call
        inside_async_with = False
        while current in parents:
            current = parents[current]
            if isinstance(current, ast.AsyncWith):
                inside_async_with = True
                break
        assert inside_async_with, call.lineno


class _ScalarsResult:
    def __init__(self, values) -> None:
        self.values = values

    def scalars(self):
        return SimpleNamespace(all=lambda: self.values)


class _ScalarResult:
    def __init__(self, value) -> None:
        self.value = value

    def scalar(self):
        return self.value


class _Session:
    def __init__(self, results) -> None:
        self.results = iter(results)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def execute(self, _statement):
        return next(self.results)


class _InlineQuery:
    def __init__(self, query: str) -> None:
        self.query = query
        self.answers = []

    async def answer(self, results, **kwargs) -> None:
        self.answers.append((results, kwargs))


def test_inline_guard_code_escapes_hostile_account_name(monkeypatch) -> None:
    from guardbot.bot.handlers import inline

    account = SimpleNamespace(
        id=1,
        account_name="</code><b>spoof</b>",
        shared_secret="encrypted",
        group_id=None,
        is_pinned=False,
    )
    session = _Session([_ScalarsResult([account]), _ScalarsResult([])])
    monkeypatch.setattr(inline, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(inline, "decrypt_value", lambda _value: "MDEyMzQ1Njc4OWFiY2RlZg==")
    query = _InlineQuery("gc")

    asyncio.run(inline.inline_gc(query, 7))

    result = query.answers[0][0][0]
    text = result.input_message_content.message_text
    assert "</code><b>spoof</b>" not in text
    assert "&lt;/code&gt;&lt;b&gt;spoof&lt;/b&gt;" in text


def test_inline_group_escapes_hostile_account_name(monkeypatch) -> None:
    from guardbot.bot.handlers import inline

    group = SimpleNamespace(id=5, name="safe")
    account = SimpleNamespace(
        account_name="</code><a href='https://evil'>spoof</a>",
        is_pinned=False,
        shared_secret=True,
        access_token=True,
        refresh_token=True,
    )
    session = _Session([
        _ScalarsResult([group]),
        _ScalarResult(1),
        _ScalarsResult([account]),
    ])
    monkeypatch.setattr(inline, "AsyncSessionLocal", lambda: session)
    query = _InlineQuery("group")

    asyncio.run(inline.inline_showgroup(query, 7))

    result = query.answers[0][0][0]
    text = result.input_message_content.message_text
    assert "</code><a href='https://evil'>spoof</a>" not in text
    assert "&lt;/code&gt;&lt;a href=&#x27;https://evil&#x27;&gt;spoof&lt;/a&gt;" in text
