import os
import urllib.parse
import threading
import time
from collections import OrderedDict
from typing import Iterator, Dict, Tuple

from sqlmodel import SQLModel, create_engine, Session
from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.infra.secrets.manager import secrets_manager
from src.infra.config.configs import Config, database_config
from src.infra.utils.resource_naming import get_key_vault_secret_name, get_role_name, get_schema_name

# ---------------------------------------------------------------------------
# Master & Tenant Engine Pooling
# ---------------------------------------------------------------------------
# We maintain:
#   1. A singleton MASTER engine for privileged operations (schema/role management, admin reads)
#   2. A dynamic LRU cache of TENANT engines (one per organisation role) using the org role credential.
#      Each tenant engine has a larger pool size (5x master by default) to serve higher query concurrency.
#      Engines can be invalidated (e.g., after password rotation) and will be recreated lazily.
# ---------------------------------------------------------------------------

_MASTER_ENGINE: Engine | None = None
MASTER_ROLE_NAME = "master_copilot_role"
MASTER_ROLE_SECRET_NAME = "master-copilot-role-password"

# LRU cache for tenant engines: key -> (Engine, last_access_ts)
_TENANT_ENGINES: "OrderedDict[str, Engine]" = OrderedDict()
_TENANT_LOCK = threading.RLock()

# Cache for secrets (role passwords) to reduce Key Vault round trips: key -> (password, fetched_ts)
_SECRET_CACHE: Dict[str, Tuple[str, float]] = {}

# Configuration / tuning knobs (can be env overrides or derived from master config)
MAX_TENANT_ENGINES = int(os.getenv("MAX_TENANT_ENGINES", "50"))
SECRET_TTL_SECONDS = int(os.getenv("TENANT_SECRET_TTL_SECONDS", "300"))  # 5 minutes
TENANT_POOL_MULTIPLIER = float(os.getenv("TENANT_POOL_MULTIPLIER", "5"))

def _now() -> float:
    return time.time()


def _get_secret_from_key_vault(secret_name: str) -> str | None:  # legacy sync shim
    cached = _SECRET_CACHE.get(secret_name)
    if cached:
        value, ts = cached
        if _now() - ts < SECRET_TTL_SECONDS:
            return value
    try:
        import asyncio
        # If already in an event loop this will raise, so we fallback to creating a new task via run
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            # Run the coroutine in a new thread to avoid blocking
            value = asyncio.run(secrets_manager.get(secret_name))  # type: ignore
        else:
            value = asyncio.run(secrets_manager.get(secret_name))
        if value:
            _SECRET_CACHE[secret_name] = (value, _now())
        return value
    except Exception as e:  # pragma: no cover
        print(f"Secret fetch failed (shim) for {secret_name}: {e}")
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


def _resolve_master_credentials() -> Tuple[str, str]:
    """Determines which credentials to use for master engine.

    Preference order:
      1. Key Vault secret for master_copilot_role (if retrievable)
      2. Environment / config DATABASE_USER + DATABASE_PASSWORD
    Raises if neither available.
    """
    # Use credentials directly from environment variables
    user = database_config.DATABASE_USER
    password = database_config.DATABASE_PASSWORD
    if user and password:
        return user, password
    raise ValueError("No master DB credentials available (DATABASE_USER and/or DATABASE_PASSWORD not set).")


def get_master_engine(echo: bool = True) -> Engine:
    """Returns (and creates if needed) the singleton master engine.

    Uses master_copilot_role from Key Vault when possible so application doesn't
    need raw superuser credentials at runtime.
    """
    global _MASTER_ENGINE
    if _MASTER_ENGINE is not None:
        return _MASTER_ENGINE

    host, port = _get_env_host_port()
    db_name = database_config.DATABASE_NAME
    user, password = _resolve_master_credentials()

    pw = urllib.parse.quote_plus(password)
    url = f"postgresql://{user}:{pw}@{host}:{port}/{db_name}"

    _MASTER_ENGINE = create_engine(
        url,
        echo=echo,
        pool_pre_ping=True,
        pool_recycle=database_config.POOL_RECYCLE_SEC,
        pool_size=database_config.DATABASE_POOL_SIZE,
        max_overflow=database_config.DATABASE_MAX_OVERFLOW,
        connect_args=_make_connect_args(),
    )
    return _MASTER_ENGINE


def reload_master_engine():
    """Disposes and recreates the master engine (e.g., after rotating master role password)."""
    global _MASTER_ENGINE
    if _MASTER_ENGINE is not None:
        try:
            _MASTER_ENGINE.dispose()
        except Exception:
            pass
    _MASTER_ENGINE = None
    # Clear cached secret so it refreshes
    _SECRET_CACHE.pop(MASTER_ROLE_SECRET_NAME, None)
    return get_master_engine(echo=False)


def _make_tenant_engine(role_name: str, password: str) -> Engine:
    host, port = _get_env_host_port()
    db_name = database_config.DATABASE_NAME
    pw = urllib.parse.quote_plus(password)
    url = f"postgresql://{role_name}:{pw}@{host}:{port}/{db_name}"

    # Tenant pool size ~ 5x master (configurable)
    base_size = database_config.DATABASE_POOL_SIZE or 5
    tenant_pool_size = max(1, int(base_size * TENANT_POOL_MULTIPLIER))
    tenant_overflow = max(0, int((database_config.DATABASE_MAX_OVERFLOW or 0) * TENANT_POOL_MULTIPLIER))

    return create_engine(
        url,
        echo=False,
        pool_pre_ping=True,
        pool_recycle=database_config.POOL_RECYCLE_SEC,
        pool_size=tenant_pool_size,
        max_overflow=tenant_overflow,
        connect_args=_make_connect_args(),
    )


def _touch_lru(key: str, engine: Engine):
    # Move key to end (most recently used)
    _TENANT_ENGINES.pop(key, None)
    _TENANT_ENGINES[key] = engine
    # Evict least recently used if over capacity
    while len(_TENANT_ENGINES) > MAX_TENANT_ENGINES:
        old_key, old_engine = _TENANT_ENGINES.popitem(last=False)
        try:
            old_engine.dispose()
        except Exception:
            pass


def get_tenant_engine(organisation_id: str) -> Engine:
    """Returns an engine logged in directly as the organisation role.

    Creates (and caches) the engine by retrieving the role password from Key Vault.
    LRU eviction keeps memory/connection growth bounded.
    """
    role_name = get_role_name(organisation_id)
    if not role_name.isidentifier():  # basic safeguard
        raise ValueError(f"Invalid derived role name: {role_name}")

    with _TENANT_LOCK:
        existing = _TENANT_ENGINES.get(role_name)
        if existing:
            _touch_lru(role_name, existing)
            return existing

        secret_name = get_key_vault_secret_name(organisation_id)
        password = _get_secret_from_key_vault(secret_name)
        if not password:
            raise RuntimeError(f"Password secret '{secret_name}' not found for organisation {organisation_id}")

        engine = _make_tenant_engine(role_name, password)
        _touch_lru(role_name, engine)
        return engine


def invalidate_tenant_engine(organisation_id: str):
    """Disposes the cached tenant engine for an organisation if present (e.g., after password rotation)."""
    role_name = get_role_name(organisation_id)
    with _TENANT_LOCK:
        engine = _TENANT_ENGINES.pop(role_name, None)
        if engine:
            try:
                engine.dispose()
            except Exception:
                pass
        # Remove cached secret so next fetch refreshes
        secret_name = get_key_vault_secret_name(organisation_id)
        _SECRET_CACHE.pop(secret_name, None)


def get_org_session(org_id: str, direct: bool = True) -> Iterator[Session]:
    """Yields a tenant session with a controlled search_path.

    Strategy:
      * Always ensure search_path includes the tenant schema THEN public (shared/master tables).
      * Prefer direct per-tenant engine (role user) if available unless overridden.
      * Fallback to SET ROLE on master connection when direct fails or in local env.

    This avoids errors like relation "users" does not exist when auth code (shared table) executes
    inside a tenant context, because the shared tables live in public (or master) schema.
    """
    if not org_id:
        raise ValueError("org_id required")

    tenant_schema = get_schema_name(org_id)
    is_local_env = Config().env == "local"
    use_set_role = is_local_env or not direct

    # Helper to apply & reset search_path safely
    def _with_search_path(sess: Session):
        # Prepend tenant schema then public. If tenant schema missing, creation will surface elsewhere.
        sess.execute(text(f'SET search_path TO "{tenant_schema}", public'))
        try:
            yield sess
        finally:
            # Reset to default (public). DISCARD ALL is expensive; explicit set is fine.
            try:
                sess.execute(text('SET search_path TO public'))
            except Exception:
                pass

    if not use_set_role:
        try:
            engine = get_tenant_engine(org_id)
            with Session(engine) as session:
                yield from _with_search_path(session)
            return
        except Exception as e:
            print(f"Direct tenant connection failed for org {org_id}, falling back to SET ROLE. Error: {e}")

    role_name = get_role_name(org_id)
    if not role_name.isidentifier():
        raise ValueError(f"Invalid derived role name: {role_name}")
    engine = get_master_engine(echo=False)
    session = Session(engine)
    try:
        session.execute(text(f'SET ROLE "{role_name}"'))
        yield from _with_search_path(session)
    finally:
        try:
            session.execute(text("RESET ROLE"))
        except Exception:
            pass
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
    """FastAPI dependency-friendly generator that yields a master session.

    Ensures we always operate on the public (shared) schema and do **not** retain
    stateful ROLE / search_path changes between requests.
    """
    engine = get_master_engine(echo=False)
    with Session(engine) as session:
        try:
            # Defensive: reset any inherited role just in case connection object was reused
            session.execute(text('RESET ROLE'))
        except Exception:
            pass
        try:
            # Be explicit so downstream auth queries reliably find shared tables.
            session.execute(text('SET search_path TO public'))
        except Exception:
            pass
        yield session

# Backwards compatible alias for dependency injection
def master_session_dep():
    return get_master_session()


def database_previlage_connectin():
    """
    Returns the master engine.
    """
    return get_master_engine(echo=False)

# Backwards compatibility alias (typo retained in existing imports)
database_privilege_connection = database_previlage_connectin
