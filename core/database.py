import os
import urllib.parse
from sqlmodel import SQLModel, create_engine, Session
from sqlalchemy import text


def _get_env_host_port():
    host = os.getenv("DATABASE_HOST", "localhost")
    port = os.getenv("DATABASE_PORT", "5432")
    return host, port


def _create_database_if_missing(host: str, port: str, user: str, password: str, db_name: str):
    """
    Connects to the 'postgres' db and issues CREATE DATABASE <db_name>.
    Ignores errors if it already exists.
    """
    pw = urllib.parse.quote_plus(password or "")
    url = f"postgresql://{user}:{pw}@{host}:{port}/postgres"
    engine = create_engine(url, isolation_level="AUTOCOMMIT", echo=False)
    with engine.connect() as conn:
        try:
            conn.execute(text(f"CREATE DATABASE {db_name}"))
        except Exception:
            pass
    engine.dispose()


def get_master_engine(echo: bool = True, create_if_missing: bool = False):
    """
    Engine for your central 'master' DB, reading creds from env.
    """
    host, port = _get_env_host_port()
    user = os.getenv("DATABASE_USER", "postgres")
    password = os.getenv("DATABASE_PASSWORD", "")
    db_name = os.getenv("DATABASE_NAME", "postgres")

    if create_if_missing:
        _create_database_if_missing(host, port, user, password, db_name)

    pw = urllib.parse.quote_plus(password or "")
    url = f"postgresql://{user}:{pw}@{host}:{port}/{db_name}"
    return create_engine(url, echo=echo)


def init_master_db(create_if_missing: bool = False):
    """
    Optionally CREATE the master DB, then create all tables on it.
    Returns the master engine.
    """
    engine = get_master_engine(echo=True, create_if_missing=create_if_missing)
    SQLModel.metadata.create_all(engine)
    return engine


def get_org_engine(
    db_name: str,
    user: str,
    password: str,
    echo: bool = True,
    create_if_missing: bool = False,
):
    """
    Engine for a per-organization DB.
    """
    host, port = _get_env_host_port()
    if create_if_missing:
        _create_database_if_missing(host, port, user, password, db_name)

    pw = urllib.parse.quote_plus(password or "")
    url = f"postgresql://{user}:{pw}@{host}:{port}/{db_name}"
    return create_engine(url, echo=echo)


def init_org_db(db_name: str, user: str, password: str, create_if_missing: bool = False):
    """
    Optionally CREATE the org DB, then create all tables on it.
    Returns the org engine.
    """
    engine = get_org_engine(
        db_name=db_name,
        user=user,
        password=password,
        echo=True,
        create_if_missing=create_if_missing,
    )
    SQLModel.metadata.create_all(engine)
    return engine


def get_session_dependency(engine):
    """
    FastAPI dependency to yield a Session bound to `engine`.
    """

    def _get_session():
        with Session(engine) as session:
            yield session

    return _get_session


# -------------------------
# Module‑level initialization
# -------------------------

# 1) Initialize the master DB & tables immediately
MASTER_ENGINE = init_master_db(create_if_missing=False)

# 2) Export a reusable dependency to inject a Session in your routes
get_master_session = get_session_dependency(MASTER_ENGINE)
