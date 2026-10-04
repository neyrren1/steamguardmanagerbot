from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from guardbot import security
from guardbot.migrations.legacy_secrets import (
    LegacySecretMigrationError,
    migrate_legacy_secret_rows,
)


def test_legacy_plaintext_secrets_are_encrypted_in_memory(monkeypatch) -> None:
    monkeypatch.setattr(security, "fernet", Fernet(Fernet.generate_key()))
    already_encrypted = security.encrypt_value("existing-secret")
    user = SimpleNamespace(general_proxy="http://user:pass@proxy.example.com:8080")
    mafile = SimpleNamespace(
        proxy=None,
        password=already_encrypted,
        email=None,
        phone=None,
        device_id=None,
        shared_secret="plaintext-shared-secret",
        identity_secret=None,
        secret_1=None,
        serial_number=None,
        revocation_code=None,
        token_gid=None,
        uri=None,
        access_token=None,
        refresh_token=None,
        cookies_encrypted='{"sessionid": "legacy"}',
        session_data_encrypted=None,
        notes=None,
    )

    report = migrate_legacy_secret_rows([user], [mafile])

    assert report.changed_values == 3
    assert security.decrypt_value(user.general_proxy).startswith("http://user:pass@")
    assert security.decrypt_value(mafile.shared_secret) == "plaintext-shared-secret"
    assert security.decrypt_dict(mafile.cookies_encrypted) == {"sessionid": "legacy"}
    assert mafile.password == already_encrypted


def test_migration_refuses_corrupted_fernet_looking_value(monkeypatch) -> None:
    monkeypatch.setattr(security, "fernet", Fernet(Fernet.generate_key()))
    user = SimpleNamespace(general_proxy="gAAAA-corrupted-token")

    with pytest.raises(LegacySecretMigrationError):
        migrate_legacy_secret_rows([user], [])
