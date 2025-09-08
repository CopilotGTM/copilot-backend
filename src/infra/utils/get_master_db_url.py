# workflows/utils/get_master_db_url.py
import os
import urllib.parse
from dotenv import load_dotenv

load_dotenv()

DATABASE_HOST = os.getenv("DATABASE_HOST", "localhost")
DATABASE_PORT = os.getenv("DATABASE_PORT", "5432")
DATABASE_NAME = os.getenv("DATABASE_NAME", "postgres")
DATABASE_USER = os.getenv("DATABASE_USER", "postgres")
DATABASE_PASSWORD = os.getenv("DATABASE_PASSWORD", "")


def get_master_db_url():
    """Get the master database URL."""
    encoded_password = urllib.parse.quote_plus(DATABASE_PASSWORD)
    base_url = (
        os.getenv("DATABASE_URL")
        or f"postgresql://{DATABASE_USER}:{encoded_password}@{DATABASE_HOST}:{DATABASE_PORT}/{DATABASE_NAME}"
    )

    ssl_mode = os.getenv("DATABASE_SSL_MODE")
    if ssl_mode:
        # If the base_url already has query parameters, append with '&', otherwise with '?'
        separator = "&" if "?" in base_url else "?"
        return f"{base_url}{separator}sslmode={ssl_mode}"

    return base_url
