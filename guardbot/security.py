"""Extracted from the legacy bot module without behavior changes."""

from cryptography.fernet import Fernet
from dotenv import load_dotenv
from guardbot.config import logger
from typing import Dict, Optional
import json
import os
import sys

fernet: Optional[Fernet] = None
BOT_TOKEN: Optional[str] = None
DATABASE_URL: Optional[str] = None

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

    # Инициализируем или генерируем Fernet ключ
    if fernet_key:
        try:
            fernet = Fernet(fernet_key.encode())
            logger.info("✅ Fernet encryption initialized successfully")
        except Exception as e:
            print(f"❌ ОШИБКА: Неверный FERNET_KEY в .env файле!")
            print(f"   {e}")
            print("   Используйте python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
            print("   чтобы сгенерировать новый ключ")
            sys.exit(1)
    else:
        # Генерируем новый ключ
        new_key = Fernet.generate_key()
        key_str = new_key.decode()
        fernet = Fernet(new_key)

        print(f"\n{'='*60}")
        print(f"⚠️  ВНИМАНИЕ: FERNET_KEY не найден в .env!")
        print(f"   Сгенерирован новый ключ (временный):")
        print(f"   {key_str}")
        print(f"\n   ДОБАВЬТЕ его в .env файл прямо сейчас:")
        print(f"   FERNET_KEY={key_str}")
        print(f"\n   Без этого ключа при следующем запуске")
        print(f"   зашифрованные данные нельзя будет расшифровать!")
        print(f"{'='*60}\n")
        logger.warning("⚠️  FERNET_KEY не указан, сгенерирован временный ключ!")

def encrypt_value(value: Optional[str]) -> Optional[str]:
    """Шифрует строковое значение"""
    if value is None or value == '':
        return value
    try:
        return fernet.encrypt(value.encode()).decode()
    except Exception as e:
        logger.error(f"❌ Encryption error: {e}")
        return value

def decrypt_value(value: Optional[str]) -> Optional[str]:
    """Расшифровывает строковое значение"""
    if value is None or value == '':
        return value
    try:
        return fernet.decrypt(value.encode()).decode()
    except Exception:
        return value

def encrypt_dict(value: Optional[Dict]) -> Optional[str]:
    """Шифрует словарь в строку"""
    if value is None:
        return None
    try:
        json_str = json.dumps(value)
        return fernet.encrypt(json_str.encode()).decode()
    except Exception as e:
        logger.error(f"❌ Dict encryption error: {e}")
        return json.dumps(value)

def decrypt_dict(value: Optional[str]) -> Optional[Dict]:
    """Расшифровывает строку в словарь"""
    if value is None:
        return None
    try:
        decrypted = fernet.decrypt(value.encode())
        return json.loads(decrypted.decode())
    except Exception:
        try:
            return json.loads(value)
        except:
            return None
