"""Compose the bot exactly as the launchers do before any test dispatches updates."""

from guardbot.bot import routing, runtime

routing.setup_routers(runtime.dp)
