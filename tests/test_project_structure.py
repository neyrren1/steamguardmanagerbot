from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_project_is_split_into_feature_modules() -> None:
    expected = {
        "guardbot/__init__.py",
        "guardbot/__main__.py",
        "guardbot/config.py",
        "guardbot/security.py",
        "guardbot/database.py",
        "guardbot/steam/client.py",
        "guardbot/services/session_manager.py",
        "guardbot/bot/runtime.py",
        "guardbot/bot/handlers/common.py",
        "guardbot/bot/handlers/accounts.py",
        "guardbot/bot/handlers/imports.py",
        "guardbot/bot/handlers/commands.py",
        "guardbot/bot/handlers/features.py",
        "guardbot/bot/handlers/groups.py",
        "guardbot/bot/handlers/inline.py",
        "guardbot/bot/handlers/messages.py",
        "tools/decode_inspect_link.py",
    }

    missing = sorted(path for path in expected if not (ROOT / path).is_file())
    assert not missing, f"Missing modules: {missing}"


def test_legacy_duplicate_files_are_removed() -> None:
    legacy_paths = ["steamguard.py", "temp.py", "L"]
    remaining = [path for path in legacy_paths if (ROOT / path).exists()]
    assert not remaining, f"Legacy paths still present: {remaining}"


def test_entrypoint_is_small_compatibility_wrapper() -> None:
    entrypoint = ROOT / "tgbot.py"
    assert entrypoint.is_file()
    assert len(entrypoint.read_text(encoding="utf-8").splitlines()) <= 20
