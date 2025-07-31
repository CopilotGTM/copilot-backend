#!/usr/bin/env python3
"""
Super Admin Initialization Script

This script creates the initial super admin account on first deployment.
It reads the SUPER_ADMIN_EMAIL from environment variables and creates
a super admin account with a temporary password, then sends an email
with the credentials.

Usage:
    python scripts/admin/init_super_admin.py
"""

import os
import sys
from pathlib import Path
from typing import Optional

# Add the project root to Python path for module resolution
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
from sqlmodel import Session, select

from src.domain.models.master_tables import SuperAdmin
from src.infra.auth.admin_auth import admin_auth
from src.infra.auth.email_service import email_service
import urllib.parse
from sqlalchemy import create_engine
from src.infra.utils.logger import get_logger

# Load environment variables from .env file
load_dotenv()

SUPER_ADMIN_EMAIL = os.getenv("SUPER_ADMIN_EMAIL")

logger = get_logger()


def check_super_admin_exists(session: Session) -> bool:
    """
    Check if a super admin already exists in the database.

    Args:
        session: The database session.

    Returns:
        True if a super admin exists, False otherwise.
    """
    try:
        statement = select(SuperAdmin).where(SuperAdmin.role == "super_admin")
        admin = session.exec(statement).first()
        return admin is not None
    except Exception as e:
        logger.error(f"Error checking for existing super admin: {e}")
        # Assuming failure to check means we should not proceed.
        return True


def create_super_admin(session: Session) -> bool:
    """
    Create the initial super admin account.

    Reads the super admin email from environment variables, creates the account,
    and sends an email with a temporary password.

    Args:
        session: The database session.

    Returns:
        True if successful, False otherwise.
    """
    if not SUPER_ADMIN_EMAIL:
        logger.error(f"'SUPER_ADMIN_EMAIL' environment variable is not set.")
        return False

    first_name, last_name = "Super", "Admin"

    logger.info(f"Creating super admin for: {first_name} {last_name} ({SUPER_ADMIN_EMAIL})")

    try:
        admin, temp_password = admin_auth.create_super_admin(
            email=SUPER_ADMIN_EMAIL,
            first_name=first_name,
            last_name=last_name,
            session=session,
        )

        logger.info(f"Successfully created super admin with ID: {admin.admin_id}")
        logger.info(f"Email: {admin.email}")
        logger.info("Sending temporary password via email...")

        email_sent = email_service.send_super_admin_creation_email(admin.email, admin.first_name, temp_password)

        if email_sent:
            logger.info("Super admin creation email sent successfully.")
        else:
            logger.warning("Failed to send super admin creation email.")
            logger.info(f"Please manually send the temporary password '{temp_password}' to {admin.email}")
        logger.info("Super admin account created successfully.")
        return True

    except Exception as e:
        logger.error(f"Error during super admin creation: {e}")
        return False


def main():
    """
    Main function to initialize the super admin.
    """
    logger.info("Starting super admin initialization script...")

    # Database connection setup
    DATABASE_HOST = os.getenv("DATABASE_HOST", "localhost")
    DATABASE_PORT = os.getenv("DATABASE_PORT", "5432")
    DATABASE_NAME = os.getenv("DATABASE_NAME", "postgres")
    DATABASE_USER = os.getenv("DATABASE_USER", "postgres")
    DATABASE_PASSWORD = os.getenv("DATABASE_PASSWORD", "")

    if not DATABASE_USER or not DATABASE_PASSWORD:
        logger.error("DATABASE_USER and DATABASE_PASSWORD must be set in environment variables.")
        sys.exit(1)

    encoded_password = urllib.parse.quote_plus(DATABASE_PASSWORD)
    db_url = f"postgresql://{DATABASE_USER}:{encoded_password}@{DATABASE_HOST}:{DATABASE_PORT}/{DATABASE_NAME}"

    engine = create_engine(db_url, echo=False)

    try:
        with Session(engine) as session:
            if check_super_admin_exists(session):
                logger.info("Super admin already exists. Skipping creation.")
                return

            logger.info("Creating new super admin account...")
            if create_super_admin(session):
                logger.info("Super admin initialization completed successfully!")
            else:
                logger.error("Super admin initialization failed.")
                sys.exit(1)

    except Exception as e:
        logger.error(f"A critical error occurred during super admin initialization: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
