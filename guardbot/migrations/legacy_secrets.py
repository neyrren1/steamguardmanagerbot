"""Opt-in migration for values stored before strict Fernet enforcement."""

import argparse
import asyncio
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from guardbot import database, security


class LegacySecretMigrationError(RuntimeError):
    """Raised when a value cannot be safely classified for migration."""


@dataclass(frozen=True)
class MigrationReport:
    changed_values: int


_USER_TEXT_FIELDS = ("general_proxy",)
_MAFILE_TEXT_FIELDS = (
    "proxy",
    "password",
    "email",
    "phone",
    "device_id",
    "shared_secret",
    "identity_secret",
    "secret_1",
    "serial_number",
    "revocation_code",
    "token_gid",
    "uri",
    "access_token",
    "refresh_token",
    "notes",
)
_MAFILE_DICT_FIELDS = ("cookies_encrypted", "session_data_encrypted")


def migrate_legacy_secret_rows(
    users: Iterable[Any], mafiles: Iterable[Any]
) -> MigrationReport:
    """Encrypt legacy plaintext fields in ORM-like rows without committing them."""
    changed = 0
    for user in users:
        changed += _migrate_text_fields(user, _USER_TEXT_FIELDS)
    for mafile in mafiles:
        changed += _migrate_text_fields(mafile, _MAFILE_TEXT_FIELDS)
        changed += _migrate_dict_fields(mafile, _MAFILE_DICT_FIELDS)
    return MigrationReport(changed_values=changed)


def _migrate_text_fields(row: Any, fields: tuple[str, ...]) -> int:
    changed = 0
    for field in fields:
        value = getattr(row, field, None)
        if value in (None, ""):
            continue
        try:
            security.decrypt_value(value)
            continue
        except security.EncryptionError:
            _reject_corrupted_fernet(value, field)
        setattr(row, field, security.encrypt_value(value))
        changed += 1
    return changed


def _migrate_dict_fields(row: Any, fields: tuple[str, ...]) -> int:
    changed = 0
    for field in fields:
        value = getattr(row, field, None)
        if value in (None, ""):
            continue
        try:
            security.decrypt_dict(value)
            continue
        except security.EncryptionError:
            _reject_corrupted_fernet(value, field)
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise LegacySecretMigrationError(
                f"Legacy field {field} is neither Fernet ciphertext nor JSON"
            ) from exc
        if not isinstance(decoded, dict):
            raise LegacySecretMigrationError(
                f"Legacy field {field} is not a JSON object"
            )
        setattr(row, field, security.encrypt_dict(decoded))
        changed += 1
    return changed


def _reject_corrupted_fernet(value: str, field: str) -> None:
    if value.startswith("gAAAA"):
        raise LegacySecretMigrationError(
            f"Field {field} resembles corrupted Fernet ciphertext; migration stopped"
        )


async def run_database_migration(*, apply: bool) -> MigrationReport:
    """Scan the configured database and optionally commit the migration."""
    async with database.AsyncSessionLocal() as session:
        users = list((await session.execute(select(database.User))).scalars().all())
        mafiles = list((await session.execute(select(database.Mafile))).scalars().all())
        report = migrate_legacy_secret_rows(users, mafiles)
        if apply:
            await session.commit()
        else:
            await session.rollback()
        return report


async def _async_main(apply: bool) -> None:
    security.init_fernet()
    database.init_engine()
    try:
        report = await run_database_migration(apply=apply)
        mode = "applied" if apply else "dry-run"
        print(f"Legacy secret migration {mode}: {report.changed_values} value(s)")
    finally:
        if database.engine is not None:
            await database.engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Encrypt legacy plaintext secrets without exposing their values"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="commit changes; without this flag the transaction is rolled back",
    )
    args = parser.parse_args()
    asyncio.run(_async_main(apply=args.apply))


if __name__ == "__main__":
    main()
