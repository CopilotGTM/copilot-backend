import os
import urllib.parse
from sqlmodel import SQLModel, create_engine, Session
from sqlalchemy import text
from sqlalchemy.engine import Engine

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

from src.infra.config.configs import database_config, key_vault_config


# Single engine for all connections (master login)
_ENGINE: Engine | None = None


def _get_secret_from_key_vault(secret_name: str) -> str | None:
    """Fetches a secret from Azure Key Vault."""
    key_vault_url = key_vault_config.AZURE_KEY_VAULT_URL
    if not key_vault_url:
        print(f"AZURE_KEY_VAULT_URL not set. Cannot fetch secret '{secret_name}'.")
        return None
    
    try:
        credential = DefaultAzureCredential()
        client = SecretClient(vault_url=key_vault_url, credential=credential)
        secret = client.get_secret(secret_name)
        return secret.value
    except Exception as e:
        print(f"Failed to fetch secret '{secret_name}' from Key Vault: {e}")
        return None


def _get_env_host_port():
    host = database_config.DATABASE_HOST
    port = database_config.DATABASE_PORT
    return host, port


def _make_connect_args():
    # allow override via env, but default to requiring SSL
    sslmode = database_config.DATABASE_SSL_MODE
    args = {"sslmode": sslmode}
    # if you have a CA bundle, point to it:
    if ca := database_config.DATABASE_SSL_ROOT_CERT:
        args["sslrootcert"] = ca
    return args


def get_master_engine(echo: bool = True) -> Engine:
    """
    Returns the singleton SQLAlchemy engine connected as the master login.
    """
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE

    host, port = _get_env_host_port()
    db_name = database_config.DATABASE_NAME
    user = database_config.DATABASE_USER
    password = database_config.DATABASE_PASSWORD

    if not user or not password:
        raise ValueError("DATABASE_USER and DATABASE_PASSWORD must be configured.")

    pw = urllib.parse.quote_plus(password)
    url = f"postgresql://{user}:{pw}@{host}:{port}/{db_name}"

    _ENGINE = create_engine(
        url,
        echo=echo,
        pool_pre_ping=True,
        pool_recycle=database_config.POOL_RECYCLE_SEC,
        pool_size=database_config.DATABASE_POOL_SIZE,
        max_overflow=database_config.DATABASE_MAX_OVERFLOW,
        connect_args=_make_connect_args(),
    )
    return _ENGINE


def get_org_session(org_id: str):
    """
    Yields a Session with SET ROLE <org_id> applied for the duration.
    Uses the singleton master engine and switches role per session.
    """
    if not org_id.isidentifier():
        raise ValueError(f"Invalid org identifier: {org_id}")

    engine = get_master_engine(echo=False)
    session = Session(engine)
    session.execute(text(f"SET ROLE {org_id}"))
    try:
        yield session
    finally:
        session.execute(text("RESET ROLE"))
        session.close()


def init_master_db():
    """Initializes the master schema (creates all tables in master schema)."""
    engine = get_master_engine(echo=False)
    SQLModel.metadata.create_all(engine)
    return engine

def init_organization_db(org_id: str):
    """
    Initializes the org schema (creates all tables in org's schema).
    Uses SET ROLE to switch to org_id before creating tables.
    """
    engine = get_master_engine(echo=False)
    with Session(engine) as session:
        session.execute(text(f"SET ROLE {org_id}"))
        SQLModel.metadata.create_all(session.connection())
        session.execute(text("RESET ROLE"))
    return engine


def get_session_dependency(engine):
    def _get_session():
        with Session(engine) as session:
            yield session

    return _get_session

def get_master_session():
    return None


def database_previlage_connectin():
    """
    Returns the master engine.
    """
    return get_master_engine(echo=False)
