"""Security configuration and encryption helpers."""

import json
import os
import sys

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

from guardbot.config import logger

fernet: Fernet | None = None
BOT_TOKEN: str | None = None
DATABASE_URL: str | None = None


class EncryptionError(RuntimeError):
    """Raised when protected data cannot be encrypted or decrypted safely."""


def _require_fernet() -> Fernet:
    if fernet is None:
        raise EncryptionError("Fernet encryption is not initialized")
    return fernet

# ==================== ФУНКЦИИ ШИФРОВАНИЯ ====================

def init_fernet():
    """Инициализация Fernet и загрузка переменных окружения"""
    global fernet, BOT_TOKEN, DATABASE_URL

    # Загружаем .env файл
    load_dotenv()

    # Читаем секреты из переменных окружения
    BOT_TOKEN = os.getenv('BOT_TOKEN')
    DATABASE_URL = os.getenv('DATABASE_URL')
    fernet_key = os.getenv('FERNET_KEY')

    # Проверяем обязательные переменные
    if not BOT_TOKEN:
        print("❌ ОШИБКА: Не указан BOT_TOKEN в .env файле!")
        print("   Создайте файл .env с переменной BOT_TOKEN=ваш_токен")
        sys.exit(1)

    if not DATABASE_URL:
        print("❌ ОШИБКА: Не указан DATABASE_URL в .env файле!")
        print("   Создайте файл .env с переменной DATABASE_URL=url_базы")
        sys.exit(1)

    # Инициализируем Fernet. Временный ключ недопустим: после рестарта
    # зашифрованные им данные было бы невозможно восстановить.
    if fernet_key:
        try:
            fernet = Fernet(fernet_key.encode())
            logger.info("✅ Fernet encryption initialized successfully")
        except (TypeError, ValueError) as e:
            print("❌ ОШИБКА: Неверный FERNET_KEY в .env файле!")
            print(f"   {e}")
            print("   Используйте python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
            print("   чтобы сгенерировать новый ключ")
            sys.exit(1)
    else:
        print("❌ ОШИБКА: Не указан FERNET_KEY в .env файле!")
        print("   Сгенерируйте постоянный ключ локально и сохраните его в FERNET_KEY.")
        sys.exit(1)

def encrypt_value(value: str | None) -> str | None:
    """Шифрует строковое значение"""
    if value is None or value == '':
        return value
    try:
        return _require_fernet().encrypt(value.encode()).decode()
    except EncryptionError:
        raise
    except Exception as e:
        logger.error("Encryption failed", exc_info=True)
        raise EncryptionError("Unable to encrypt protected value") from e

def decrypt_value(value: str | None) -> str | None:
    """Расшифровывает строковое значение"""
    if value is None or value == '':
        return value
    try:
        return _require_fernet().decrypt(value.encode()).decode()
    except EncryptionError:
        raise
    except (InvalidToken, ValueError, TypeError) as e:
        raise EncryptionError("Protected value is not valid Fernet ciphertext") from e
    except Exception as e:
        logger.error("Decryption failed", exc_info=True)
        raise EncryptionError("Unable to decrypt protected value") from e

def encrypt_dict(value: dict | None) -> str | None:
    """Шифрует словарь в строку"""
    if value is None:
        return None
    try:
        json_str = json.dumps(value)
        return _require_fernet().encrypt(json_str.encode()).decode()
    except EncryptionError:
        raise
    except Exception as e:
        logger.error("Dictionary encryption failed", exc_info=True)
        raise EncryptionError("Unable to encrypt protected dictionary") from e

def decrypt_dict(value: str | None) -> dict | None:
    """Расшифровывает строку в словарь"""
    if value is None:
        return None
    try:
        decrypted = _require_fernet().decrypt(value.encode())
        result = json.loads(decrypted.decode())
        if not isinstance(result, dict):
            raise EncryptionError("Decrypted value is not a dictionary")
        return result
    except EncryptionError:
        raise
    except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as e:
        raise EncryptionError("Protected dictionary is invalid") from e
    except Exception as e:
        logger.error("Dictionary decryption failed", exc_info=True)
        raise EncryptionError("Unable to decrypt protected dictionary") from e
