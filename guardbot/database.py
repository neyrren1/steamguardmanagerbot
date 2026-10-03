"""Extracted from the legacy bot module without behavior changes."""

from datetime import datetime
from guardbot import security
from guardbot.config import logger
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, JSON, String, Text, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from typing import List, Optional

# ==================== БАЗА ДАННЫХ ====================
class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    general_proxy: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bot_password: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_activity: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    lock_timeout: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    mafiles: Mapped[list["Mafile"]] = relationship("Mafile", back_populates="user", cascade="all, delete-orphan")
    groups: Mapped[list["AccountGroup"]] = relationship("AccountGroup", cascade="all, delete-orphan")


class AccountGroup(Base):
    __tablename__ = "account_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    mafiles: Mapped[list["Mafile"]] = relationship("Mafile", back_populates="group")


class Mafile(Base):
    __tablename__ = "mafiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id", ondelete="CASCADE"), nullable=False, index=True)
    unique_proxy: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    proxy: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    account_name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password: Mapped[str] = mapped_column(Text, nullable=False)
    steamid: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    email: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    device_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    shared_secret: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    identity_secret: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    secret_1: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    serial_number: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    revocation_code: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    token_gid: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    uri: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    access_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cookies_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    session_data_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    fully_enrolled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    token_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    trade_notifications: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    trade_notify_interval: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_trade_check: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    known_trade_ids: Mapped[Optional[List[str]]] = mapped_column(JSON, nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(2), nullable=True)
    currency_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # 🔥 Новые поля
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    group_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("account_groups.id", ondelete="SET NULL"), nullable=True)

    user: Mapped["User"] = relationship("User", back_populates="mafiles")
    group: Mapped[Optional["AccountGroup"]] = relationship("AccountGroup", back_populates="mafiles")

# Глобальные объекты БД конфигурируются в main(), но сохраняют identity.
class SessionFactoryProxy:
    def __init__(self) -> None:
        self._factory = None

    def configure(self, factory) -> None:
        self._factory = factory

    def __call__(self, *args, **kwargs):
        if self._factory is None:
            raise RuntimeError("Database session factory is not initialized")
        return self._factory(*args, **kwargs)


engine = None
AsyncSessionLocal = SessionFactoryProxy()

def init_engine():
    """Инициализация подключения к БД (вызывается после init_fernet)"""
    global engine
    engine = create_async_engine(
        security.DATABASE_URL,
        echo=True,
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_size=10,
        max_overflow=20,
    )
    AsyncSessionLocal.configure(async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession))
    logger.info("✅ Database engine initialized")

async def init_db():
    """Создание и обновление таблиц в базе данных"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        try:
            await conn.execute(text("""
                DO $$
                BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                  WHERE table_name='mafiles' AND column_name='session_data_encrypted') THEN
                        ALTER TABLE mafiles ADD COLUMN session_data_encrypted TEXT;
                    END IF;

                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                  WHERE table_name='mafiles' AND column_name='cookies_encrypted') THEN
                        ALTER TABLE mafiles ADD COLUMN cookies_encrypted TEXT;
                    END IF;

                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                  WHERE table_name='mafiles' AND column_name='fully_enrolled') THEN
                        ALTER TABLE mafiles ADD COLUMN fully_enrolled BOOLEAN DEFAULT FALSE;
                    END IF;

                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                  WHERE table_name='mafiles' AND column_name='token_expires_at') THEN
                        ALTER TABLE mafiles ADD COLUMN token_expires_at TIMESTAMP;
                    END IF;

                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                  WHERE table_name='mafiles' AND column_name='trade_notifications') THEN
                        ALTER TABLE mafiles ADD COLUMN trade_notifications BOOLEAN DEFAULT FALSE;
                    END IF;

                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                  WHERE table_name='mafiles' AND column_name='trade_notify_interval') THEN
                        ALTER TABLE mafiles ADD COLUMN trade_notify_interval INTEGER;
                    END IF;

                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                  WHERE table_name='mafiles' AND column_name='last_trade_check') THEN
                        ALTER TABLE mafiles ADD COLUMN last_trade_check TIMESTAMP;
                    END IF;

                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                  WHERE table_name='mafiles' AND column_name='known_trade_ids') THEN
                        ALTER TABLE mafiles ADD COLUMN known_trade_ids JSONB;
                    END IF;

                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                  WHERE table_name='mafiles' AND column_name='country') THEN
                        ALTER TABLE mafiles ADD COLUMN country VARCHAR(2);
                    END IF;

                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                  WHERE table_name='mafiles' AND column_name='currency_id') THEN
                        ALTER TABLE mafiles ADD COLUMN currency_id INTEGER;
                    END IF;

                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                  WHERE table_name='users' AND column_name='bot_password') THEN
                        ALTER TABLE users ADD COLUMN bot_password VARCHAR(255);
                    END IF;

                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                  WHERE table_name='users' AND column_name='last_activity') THEN
                        ALTER TABLE users ADD COLUMN last_activity TIMESTAMP;
                    END IF;

                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                  WHERE table_name='users' AND column_name='lock_timeout') THEN
                        ALTER TABLE users ADD COLUMN lock_timeout INTEGER;
                    END IF;

                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                  WHERE table_name='mafiles' AND column_name='is_pinned') THEN
                        ALTER TABLE mafiles ADD COLUMN is_pinned BOOLEAN DEFAULT FALSE;
                    END IF;

                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                  WHERE table_name='mafiles' AND column_name='sort_order') THEN
                        ALTER TABLE mafiles ADD COLUMN sort_order INTEGER DEFAULT 0;
                    END IF;

                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                                  WHERE table_name='mafiles' AND column_name='group_id') THEN
                        ALTER TABLE mafiles ADD COLUMN group_id INTEGER;
                    END IF;
                END $$;
            """))
        except Exception as e:
            logger.warning(f"Ошибка при добавлении колонок (возможно они уже существуют): {e}")

    logger.info("Таблицы созданы/обновлены успешно")

async def save_user(telegram_id: int, username: str, full_name: str):
    """Сохранение или обновление информации о пользователе"""
    async with AsyncSessionLocal() as session:
        stmt = select(User).where(User.telegram_id == telegram_id)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()

        if user:
            user.username = username
            user.full_name = full_name
            user.last_seen = datetime.utcnow()
            logger.info(f"Пользователь обновлен: {telegram_id}")
        else:
            user = User(
                telegram_id=telegram_id,
                username=username,
                full_name=full_name
            )
            session.add(user)
            logger.info(f"Новый пользователь сохранен: {telegram_id}")

        await session.commit()
        return user
