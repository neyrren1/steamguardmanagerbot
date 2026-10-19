"""Application entrypoint."""

import asyncio

from guardbot import database, security
from guardbot.bot import routing, runtime


async def main() -> None:
    security.init_fernet()
    database.init_engine()
    runtime.init_bot()
    routing.setup_routers(runtime.dp)

    runtime.logger.info("Инициализация базы данных...")
    await database.init_db()
    await runtime.init_trade_check_tasks()

    runtime.logger.info("Запуск бота...")
    await runtime.dp.start_polling(runtime.bot.get())


if __name__ == "__main__":
    asyncio.run(main())
