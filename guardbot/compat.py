"""Compatibility exports for the historical ``tgbot`` module."""

from guardbot.config import *
from guardbot.steam.client import *
from guardbot.security import *
from guardbot.database import *
from guardbot.services.session_manager import *
from guardbot.bot.runtime import *

# Historical names only: dispatch order is defined by guardbot.bot.routing,
# not by the order of these imports.
# isort: off
from guardbot.bot.handlers.common import *
from guardbot.bot.handlers.imports import *
from guardbot.bot.handlers.accounts import *
from guardbot.bot.handlers.commands import *
from guardbot.bot.handlers.bulk_import import *
from guardbot.bot.handlers.features import *
from guardbot.bot.handlers.groups import *
from guardbot.bot.handlers.inline import *
from guardbot.bot.handlers.rich_demo import *
from guardbot.bot.handlers.messages import *
# isort: on

# Names that used to reach this namespace only through incidental imports in
# guardbot.bot.handlers.accounts; kept explicit so the tgbot API stays stable.
from guardbot.bot.pending_challenges import (  # noqa: E402,F401
    lease_pending_challenge,
    pending_challenge_is_leased,
    publish_pending_challenge,
    read_pending_challenge_data,
    release_pending_challenge,
)
from typing import cast  # noqa: E402,F401

# ``router`` and the imported aiogram ``Router`` type are implementation
# details of handler modules, not historical tgbot API names. Every wildcard
# import above overwrites them; remove the final aliases from the compatibility
# namespace instead of exposing the catch-all router as ``tgbot.router``.
globals().pop("router", None)
globals().pop("Router", None)
