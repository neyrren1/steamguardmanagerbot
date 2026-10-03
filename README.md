# Steam Guard Manager Bot

Telegram-бот для управления Steam-аккаунтами, кодами Steam Guard, подтверждениями, трейдами и инвентарём.

## Структура

- `guardbot/steam/client.py` — асинхронный Steam-клиент.
- `guardbot/security.py` — загрузка конфигурации и шифрование.
- `guardbot/database.py` — модели SQLAlchemy и инициализация БД.
- `guardbot/services/session_manager.py` — сохранение и восстановление Steam-сессий.
- `guardbot/bot/runtime.py` — общие объекты aiogram и фоновые сервисы.
- `guardbot/bot/handlers/` — обработчики, разделённые по функциональным областям.
- `tools/decode_inspect_link.py` — отдельная CLI-утилита декодирования CS2 inspect-ссылок.
- `tgbot.py` — совместимая точка запуска.

## Настройка

Требуется Python 3.11 или новее. Рекомендуемый способ установки через `uv`:

```bash
uv sync --dev
```

В локальном `.env` должны быть заданы:

```dotenv
BOT_TOKEN=...
DATABASE_URL=postgresql+asyncpg://...
FERNET_KEY=...
```

Не коммить `.env`, `accs.txt`, maFile и другие данные аккаунтов.

## Запуск

```bash
uv run python tgbot.py
```

Windows-ярлык `start.bat` оставлен для совместимости.

Генерация Fernet-ключа:

```bash
uv run python tgbot.py --generate-key
```

## Проверки

```bash
uv run pytest
uv run python -m compileall -q guardbot tgbot.py tools tests
```
