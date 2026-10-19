"""Compose the production router tree once for dispatcher-level tests.

The module routers are single-parent aiogram objects. Tests that need an
independent Dispatcher must create their own local routers rather than trying
to attach ``routing.ROUTERS`` again.
"""

from guardbot.bot import routing, runtime

routing.setup_routers(runtime.dp)
