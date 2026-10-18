"""Reusable filters for bot message handlers."""

from aiogram.filters import Filter
from aiogram.types import Message


class NonCommandInputFilter(Filter):
    """Let slash commands continue to registered command handlers."""

    async def __call__(self, message: Message) -> bool:
        content = message.text or message.caption or ""
        return not content.startswith("/")
