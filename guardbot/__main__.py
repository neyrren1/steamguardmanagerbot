"""Application entrypoint."""

import asyncio

from guardbot import database, security
from guardbot.bot import runtime

# Registration order is significant: the unfiltered message handler must stay last.
# isort: off
from guardbot.bot.handlers import (  # noqa: F401 - imports register handlers
    accounts,
    bulk_import,
    commands,
    common,
    features,
    groups,
    imports,
    inline,
    rich_demo,
    messages,
)
# isort: on


async def main() -> None:
    security.init_fernet()
    database.init_engine()
    runtime.init_bot()

    runtime.logger.info("Инициализация базы данных...")
    await database.init_db()
    await runtime.init_trade_check_tasks()

    runtime.logger.info("Запуск бота...")
    await runtime.dp.start_polling(runtime.bot.get())


if __name__ == "__main__":
    asyncio.run(main())
