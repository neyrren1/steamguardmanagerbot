"""The legacy ``user_states`` store is gone; typed starters clear live challenges."""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[1]
LEGACY_NAMES = {
    "user_states",
    "set_legacy_user_state",
    "clear_legacy_user_state",
    "lease_legacy_user_state",
    "release_legacy_user_state_lease",
    "_SECRET_LEGACY_STATES",
}


class Client:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class FakeState:
    def __init__(self) -> None:
        self.raw_state = None
        self.data: dict = {}

    async def set_state(self, value) -> None:
        self.raw_state = value.state if hasattr(value, "state") else value

    async def set_data(self, value: dict) -> None:
        self.data = dict(value)

    async def get_state(self):
        return self.raw_state

    async def get_data(self) -> dict:
        return dict(self.data)

    async def clear(self) -> None:
        self.raw_state = None
        self.data = {}


def test_runtime_no_longer_exposes_legacy_store() -> None:
    import tgbot
    from guardbot.bot import runtime

    for name in LEGACY_NAMES:
        assert not hasattr(runtime, name), name
        assert not hasattr(tgbot, name), name


def test_production_code_has_no_legacy_store_references() -> None:
    offenders = []
    for path in (ROOT / "guardbot").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.alias):
                name = node.name
            if name in LEGACY_NAMES:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}:{name}")
    assert offenders == []


async def _publish_challenge(user_id: int, client: Client, state: FakeState) -> str:
    from guardbot.bot import pending_challenges
    from guardbot.bot.states import SteamChallengeFlow

    return await pending_challenges.publish_pending_challenge(
        user_id=user_id,
        mafile_id=42,
        kind="email",
        client=client,
        state=state,
        fsm_state=SteamChallengeFlow.waiting_code,
        ttl_seconds=60,
    )


def test_unlock_prompt_closes_pending_challenge_and_drops_its_data() -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import commands
    from guardbot.bot.states import UnlockFlow

    user_id = 701
    client = Client()

    class Message:
        from_user = SimpleNamespace(id=user_id)
        chat = SimpleNamespace(type="private")

        async def delete(self) -> None:
            return None

        async def answer(self, *_args, **_kwargs) -> None:
            return None

    async def scenario() -> None:
        state = FakeState()
        await _publish_challenge(user_id, client, state)
        await commands.cmd_unlock(Message(), SimpleNamespace(args=None), state)

        assert client.close_calls == 1
        assert pending_challenges.current_pending_challenge_token(user_id) is None
        assert await state.get_state() == UnlockFlow.waiting_password.state
        assert await state.get_data() == {}

    asyncio.run(scenario())


def test_unlock_button_closes_pending_challenge_and_drops_its_data() -> None:
    from guardbot.bot import pending_challenges
    from guardbot.bot.handlers import features
    from guardbot.bot.states import UnlockFlow

    user_id = 702
    client = Client()

    class CallbackMessage:
        chat = SimpleNamespace(type="private")

        async def edit_text(self, *_args, **_kwargs) -> None:
            return None

    class Callback:
        data = "unlock_bot"
        from_user = SimpleNamespace(id=user_id)
        message = CallbackMessage()

        async def answer(self, *_args, **_kwargs) -> None:
            return None

    async def scenario() -> None:
        state = FakeState()
        await _publish_challenge(user_id, client, state)
        await features.callback_unlock_bot(Callback(), state)

        assert client.close_calls == 1
        assert pending_challenges.current_pending_challenge_token(user_id) is None
        assert await state.get_state() == UnlockFlow.waiting_password.state
        assert await state.get_data() == {}

    asyncio.run(scenario())
