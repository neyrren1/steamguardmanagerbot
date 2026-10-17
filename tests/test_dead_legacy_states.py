from pathlib import Path

import pytest


_REMOVED_STATES = (
    "waiting_shared_secret",
    "waiting_device_id",
    "waiting_identity_secret",
    "waiting_password_for_mafile",
)


@pytest.mark.parametrize("legacy_state", _REMOVED_STATES)
def test_unreachable_manual_secret_states_are_removed(legacy_state: str) -> None:
    from guardbot.bot import runtime

    assert legacy_state not in runtime._SECRET_LEGACY_STATES


def test_removed_manual_secret_states_are_absent_from_production_sources() -> None:
    production_root = Path(__file__).parents[1] / "guardbot"
    references = {
        state: []
        for state in _REMOVED_STATES
    }
    for source_path in production_root.rglob("*.py"):
        source = source_path.read_text(encoding="utf-8")
        for state in _REMOVED_STATES:
            if state in source:
                references[state].append(str(source_path.relative_to(production_root)))

    assert references == {state: [] for state in _REMOVED_STATES}
