"""Owner-scoped database lookups for Telegram-controlled entities."""

from guardbot.database import AccountGroup, Mafile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def get_owned_mafile(
    session: AsyncSession, mafile_id: int, telegram_id: int
) -> Mafile | None:
    result = await session.execute(
        select(Mafile).where(
            Mafile.id == mafile_id,
            Mafile.telegram_id == telegram_id,
        )
    )
    return result.scalar_one_or_none()


async def get_owned_group(
    session: AsyncSession, group_id: int, telegram_id: int
) -> AccountGroup | None:
    result = await session.execute(
        select(AccountGroup).where(
            AccountGroup.id == group_id,
            AccountGroup.telegram_id == telegram_id,
        )
    )
    return result.scalar_one_or_none()
