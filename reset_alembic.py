#!/usr/bin/env python3
import os
import sys
import urllib.parse
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError


def reset_alembic():
    try:
        # Get database configuration from environment variables
        DATABASE_HOST = os.getenv("DATABASE_HOST", "localhost")
        DATABASE_PORT = os.getenv("DATABASE_PORT", "5432")
        DATABASE_NAME = os.getenv("DATABASE_NAME", "postgres")
        DATABASE_USER = os.getenv("DATABASE_USER", "postgres")
        DATABASE_PASSWORD = os.getenv("DATABASE_PASSWORD", "")

        # URL encode the password to handle special characters
        encoded_password = urllib.parse.quote_plus(DATABASE_PASSWORD)

        # Construct database URL
        db_url = f"postgresql://{DATABASE_USER}:{encoded_password}@{DATABASE_HOST}:{DATABASE_PORT}/{DATABASE_NAME}"

        print(f"Connecting to database at {DATABASE_HOST}:{DATABASE_PORT}/{DATABASE_NAME}")

        engine = create_engine(db_url)

        with engine.connect() as conn:
            # Drop the alembic_version table if it exists
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))

            # Drop the content_type_enum if it exists (need to drop dependent tables first)
            conn.execute(text("DROP TABLE IF EXISTS organisation_documents"))
            conn.execute(text("DROP TYPE IF EXISTS content_type_enum"))

            conn.commit()
            print("✅ Successfully reset database state for fresh migration")

    except SQLAlchemyError as e:
        print(f"❌ Database error: {str(e)}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    reset_alembic()
