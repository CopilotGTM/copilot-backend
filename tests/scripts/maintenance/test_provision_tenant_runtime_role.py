from __future__ import annotations

from contextlib import contextmanager

import pytest
from azure.core.exceptions import ResourceNotFoundError
from sqlalchemy.exc import NotSupportedError

from scripts.maintenance import provision_tenant_runtime_role as provision_module


class RecordingSession:
    def __init__(self, *, role_exists: bool = False) -> None:
        self.role_exists = role_exists
        self.statements: list[str] = []
        self.params: list[dict[str, str] | None] = []
        self.events: list[str] = []

    def begin(self):
        return self

    def __enter__(self):
        self.events.append("begin")
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        self.events.append("rollback" if exc_type else "commit")
        return False

    @contextmanager
    def begin_nested(self):
        yield self

    def execute(self, statement, params=None):  # noqa: ANN001
        self.statements.append(str(statement))
        self.params.append(params)
        value = 1 if self.role_exists else None
        return type("Result", (), {"scalar_one_or_none": lambda self: value})()


class AzureExtensionMoveUnsupportedSession(RecordingSession):
    @contextmanager
    def begin_nested(self):
        self.events.append("savepoint")
        try:
            yield self
        except Exception:
            self.events.append("rollback_savepoint")
            raise
        else:
            self.events.append("release_savepoint")

    def execute(self, statement, params=None):  # noqa: ANN001
        if str(statement).startswith("ALTER EXTENSION vector SET SCHEMA"):
            self.statements.append(str(statement))
            self.params.append(params)
            raise NotSupportedError(str(statement), params, Exception("SET SCHEMA unsupported"))
        return super().execute(statement, params=params)


def _configure_runtime(monkeypatch) -> None:
    monkeypatch.setattr(
        provision_module.database_config, "DATABASE_USER", "postgres", raising=False
    )
    monkeypatch.setattr(provision_module.database_config, "DATABASE_NAME", "appdb", raising=False)
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_USER",
        "app_tenant_runtime",
        raising=False,
    )
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_PASSWORD",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_PASSWORD_SECRET_NAME",
        "shared-tenant-db-password",
        raising=False,
    )


def test_build_secret_client_uses_shared_master_secret_client(monkeypatch) -> None:
    client = object()
    captured: list[bool] = []

    def fake_build_master_secret_client_sync(*, prefer_default_credential: bool = False):
        captured.append(prefer_default_credential)
        return client

    monkeypatch.setattr(
        provision_module,
        "build_master_secret_client_sync",
        fake_build_master_secret_client_sync,
        raising=False,
    )

    assert provision_module.build_secret_client() is client
    assert captured == [True]


def test_creates_dedicated_runtime_role_and_stores_password(monkeypatch) -> None:
    session = RecordingSession(role_exists=False)
    stored: list[tuple[str, str]] = []

    @contextmanager
    def fake_master_session_scope():
        yield session

    class FakeSecretClient:
        def __init__(self, vault_url: str, credential) -> None:  # noqa: ANN001
            assert vault_url == "https://master-vault.example"

        def get_secret(self, name: str):
            raise AssertionError(f"unexpected existing secret lookup for {name}")

        def set_secret(self, name: str, value: str) -> None:
            stored.append((name, value))

    monkeypatch.setattr(provision_module, "master_session_scope", fake_master_session_scope)
    monkeypatch.setattr(
        provision_module,
        "build_master_secret_client_sync",
        lambda *, prefer_default_credential=False: FakeSecretClient(
            "https://master-vault.example", object()
        ),
    )
    monkeypatch.setattr(provision_module.secrets, "token_urlsafe", lambda length: "generated")
    _configure_runtime(monkeypatch)

    provision_module.provision_runtime_role(execute=True, rotate_password=False)

    rendered_sql = "\n".join(session.statements)
    assert 'CREATE ROLE "app_tenant_runtime" LOGIN PASSWORD :pwd' in rendered_sql
    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS" in rendered_sql
    assert (
        'ALTER ROLE "app_tenant_runtime" IN DATABASE "appdb" SET search_path = pg_catalog'
        in rendered_sql
    )
    assert "REVOKE CREATE ON SCHEMA public FROM PUBLIC" in rendered_sql
    assert 'CREATE SCHEMA IF NOT EXISTS "extensions" AUTHORIZATION "postgres"' in rendered_sql
    assert 'REVOKE CREATE ON SCHEMA "extensions" FROM PUBLIC' in rendered_sql
    assert 'GRANT USAGE ON SCHEMA "extensions" TO "app_tenant_runtime"' in rendered_sql
    assert 'CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA "extensions"' in rendered_sql
    assert 'ALTER EXTENSION vector SET SCHEMA "extensions"' in rendered_sql
    assert stored == [("shared-tenant-db-password", "generated")]
    assert session.events == ["begin", "commit"]


def test_direct_password_mode_does_not_build_key_vault_client(monkeypatch) -> None:
    session = RecordingSession(role_exists=False)

    @contextmanager
    def fake_master_session_scope():
        yield session

    monkeypatch.setattr(provision_module, "master_session_scope", fake_master_session_scope)
    monkeypatch.setattr(
        provision_module,
        "build_master_secret_client_sync",
        lambda *, prefer_default_credential=False: (_ for _ in ()).throw(
            AssertionError("direct password mode must not require Key Vault")
        ),
    )
    _configure_runtime(monkeypatch)
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_PASSWORD",
        "direct-password",
        raising=False,
    )

    provision_module.provision_runtime_role(execute=True, rotate_password=False)

    rendered_sql = "\n".join(session.statements)
    assert 'CREATE ROLE "app_tenant_runtime" LOGIN PASSWORD :pwd' in rendered_sql
    assert {"pwd": "direct-password"} in session.params


def test_azure_unsupported_extension_move_does_not_abort_runtime_role_creation(
    monkeypatch,
) -> None:
    session = AzureExtensionMoveUnsupportedSession(role_exists=False)
    stored: list[tuple[str, str]] = []

    @contextmanager
    def fake_master_session_scope():
        yield session

    class FakeSecretClient:
        def set_secret(self, name: str, value: str) -> None:
            stored.append((name, value))

    monkeypatch.setattr(provision_module, "master_session_scope", fake_master_session_scope)
    monkeypatch.setattr(
        provision_module,
        "build_master_secret_client_sync",
        lambda *, prefer_default_credential=False: FakeSecretClient(),
    )
    monkeypatch.setattr(provision_module.secrets, "token_urlsafe", lambda length: "generated")
    _configure_runtime(monkeypatch)

    provision_module.provision_runtime_role(execute=True, rotate_password=False)

    rendered_sql = "\n".join(session.statements)
    assert 'CREATE ROLE "app_tenant_runtime" LOGIN PASSWORD :pwd' in rendered_sql
    assert 'ALTER EXTENSION vector SET SCHEMA "extensions"' in rendered_sql
    assert stored == [("shared-tenant-db-password", "generated")]
    assert session.events == ["begin", "savepoint", "rollback_savepoint", "commit"]


def test_existing_role_does_not_rotate_password_without_flag(monkeypatch) -> None:
    session = RecordingSession(role_exists=True)
    stored: list[tuple[str, str]] = []

    @contextmanager
    def fake_master_session_scope():
        yield session

    class FakeSecretClient:
        def __init__(self, vault_url: str, credential) -> None:  # noqa: ANN001
            assert vault_url == "https://master-vault.example"

        def get_secret(self, name: str):
            assert name == "shared-tenant-db-password"
            return type("Secret", (), {"value": "existing-password"})()

        def set_secret(self, name: str, value: str) -> None:
            stored.append((name, value))

    monkeypatch.setattr(provision_module, "master_session_scope", fake_master_session_scope)
    monkeypatch.setattr(
        provision_module,
        "build_master_secret_client_sync",
        lambda *, prefer_default_credential=False: FakeSecretClient(
            "https://master-vault.example", object()
        ),
    )
    _configure_runtime(monkeypatch)

    provision_module.provision_runtime_role(execute=True, rotate_password=False)

    rendered_sql = "\n".join(session.statements)
    assert "PASSWORD :pwd" not in rendered_sql
    assert 'ALTER ROLE "app_tenant_runtime" WITH LOGIN' in rendered_sql
    assert stored == []


def test_existing_role_fails_when_runtime_secret_missing(monkeypatch) -> None:
    session = RecordingSession(role_exists=True)

    @contextmanager
    def fake_master_session_scope():
        yield session

    class FakeSecretClient:
        def __init__(self, vault_url: str, credential) -> None:  # noqa: ANN001
            assert vault_url == "https://master-vault.example"

        def get_secret(self, name: str):
            assert name == "shared-tenant-db-password"
            return None

    monkeypatch.setattr(provision_module, "master_session_scope", fake_master_session_scope)
    monkeypatch.setattr(
        provision_module,
        "build_master_secret_client_sync",
        lambda *, prefer_default_credential=False: FakeSecretClient(
            "https://master-vault.example", object()
        ),
    )
    _configure_runtime(monkeypatch)

    with pytest.raises(RuntimeError, match="missing from Key Vault"):
        provision_module.provision_runtime_role(execute=True, rotate_password=False)

    assert "ALTER ROLE" not in "\n".join(session.statements)


def test_existing_role_treats_key_vault_not_found_as_missing(monkeypatch) -> None:
    session = RecordingSession(role_exists=True)

    @contextmanager
    def fake_master_session_scope():
        yield session

    class FakeSecretClient:
        def __init__(self, vault_url: str, credential) -> None:  # noqa: ANN001
            assert vault_url == "https://master-vault.example"

        def get_secret(self, name: str):
            assert name == "shared-tenant-db-password"
            raise ResourceNotFoundError(message="not found")

    monkeypatch.setattr(provision_module, "master_session_scope", fake_master_session_scope)
    monkeypatch.setattr(
        provision_module,
        "build_master_secret_client_sync",
        lambda *, prefer_default_credential=False: FakeSecretClient(
            "https://master-vault.example", object()
        ),
    )
    _configure_runtime(monkeypatch)

    with pytest.raises(RuntimeError, match="missing from Key Vault"):
        provision_module.provision_runtime_role(execute=True, rotate_password=False)

    assert "ALTER ROLE" not in "\n".join(session.statements)


def test_key_vault_write_failure_rolls_back_password_sql(monkeypatch) -> None:
    session = RecordingSession(role_exists=False)

    @contextmanager
    def fake_master_session_scope():
        yield session

    class FakeSecretClient:
        def __init__(self, vault_url: str, credential) -> None:  # noqa: ANN001
            assert vault_url == "https://master-vault.example"

        def set_secret(self, name: str, value: str) -> None:
            assert name == "shared-tenant-db-password"
            assert value == "generated"
            raise RuntimeError("Key Vault write failed")

    monkeypatch.setattr(provision_module, "master_session_scope", fake_master_session_scope)
    monkeypatch.setattr(
        provision_module,
        "build_master_secret_client_sync",
        lambda *, prefer_default_credential=False: FakeSecretClient(
            "https://master-vault.example", object()
        ),
    )
    monkeypatch.setattr(provision_module.secrets, "token_urlsafe", lambda length: "generated")
    _configure_runtime(monkeypatch)

    with pytest.raises(RuntimeError, match="Key Vault write failed"):
        provision_module.provision_runtime_role(execute=True, rotate_password=False)

    assert 'CREATE ROLE "app_tenant_runtime" LOGIN PASSWORD :pwd' in "\n".join(session.statements)
    assert session.events == ["begin", "rollback"]


def test_rejects_master_user_reuse(monkeypatch) -> None:
    monkeypatch.setattr(
        provision_module.database_config, "DATABASE_USER", "postgres", raising=False
    )
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_USER",
        "postgres",
        raising=False,
    )
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_PASSWORD",
        "",
        raising=False,
    )
    monkeypatch.setattr(
        provision_module.database_config,
        "TENANT_DATABASE_PASSWORD_SECRET_NAME",
        "shared-tenant-db-password",
        raising=False,
    )

    with pytest.raises(RuntimeError, match="must not equal DATABASE_USER"):
        provision_module.provision_runtime_role(execute=True, rotate_password=False)
