import os
import urllib.parse
from sqlmodel import SQLModel, create_engine, Session
from sqlalchemy import text


def _get_env_host_port():
    host = os.getenv("DATABASE_HOST", "localhost")
    port = os.getenv("DATABASE_PORT", "5432")
    return host, port


def _make_connect_args():
    # allow override via env, but default to requiring SSL
    sslmode = os.getenv("DATABASE_SSL_MODE", "require")
    args = {"sslmode": sslmode}
    # if you have a CA bundle, point to it:
    if ca := os.getenv("DATABASE_SSL_ROOT_CERT"):
        args["sslrootcert"] = ca
    return args


def _create_database_if_missing(host: str, port: str, user: str, password: str, db_name: str):
    pw = urllib.parse.quote_plus(password or "")
    url = f"postgresql://{user}:{pw}@{host}:{port}/postgres"
    engine = create_engine(
        url,
        isolation_level="AUTOCOMMIT",
        echo=False,
        # even our “bootstrap” connection should speak SSL
        connect_args=_make_connect_args(),
        pool_pre_ping=True,
    )
    with engine.connect() as conn:
        try:
            conn.execute(text(f"CREATE DATABASE {db_name}"))
        except Exception:
            pass
    engine.dispose()


def get_master_engine(echo: bool = True, create_if_missing: bool = False):
    host, port = _get_env_host_port()
    user = os.getenv("DATABASE_USER", "postgres")
    password = os.getenv("DATABASE_PASSWORD", "")
    db_name = os.getenv("DATABASE_NAME", "postgres")

    if create_if_missing:
        _create_database_if_missing(host, port, user, password, db_name)

    pw = urllib.parse.quote_plus(password or "")
    url = f"postgresql://{user}:{pw}@{host}:{port}/{db_name}"

    return create_engine(
        url,
        echo=echo,
        # test each connection’s liveness before using it:
        pool_pre_ping=True,
        # optionally recycle connections < max idle time:
        pool_recycle=int(os.getenv("POOL_RECYCLE_SEC", 1800)),
        connect_args=_make_connect_args(),
    )


def init_master_db(create_if_missing: bool = False):
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
    host, port = _get_env_host_port()
    if create_if_missing:
        _create_database_if_missing(host, port, user, password, db_name)

    pw = urllib.parse.quote_plus(password or "")
    url = f"postgresql://{user}:{pw}@{host}:{port}/{db_name}"

    return create_engine(
        url,
        echo=echo,
        pool_pre_ping=True,
        pool_recycle=int(os.getenv("POOL_RECYCLE_SEC", 1800)),
        connect_args=_make_connect_args(),
    )


def init_org_db(db_name: str, user: str, password: str, create_if_missing: bool = False):
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
    def _get_session():
        with Session(engine) as session:
            yield session

    return _get_session


# Module‑level init
MASTER_ENGINE = init_master_db(create_if_missing=False)
get_master_session = get_session_dependency(MASTER_ENGINE)
