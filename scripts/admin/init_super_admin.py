#!/usr/bin/env python3
"""
Super Admin Initialization Script

Creates the initial super admin account on first deployment using the async DB
provider and awaits the AdminAuth coroutine properly.

Usage:
    python scripts/admin/init_super_admin.py
"""

import asyncio
import os
import sys
from pathlib import Path

# Add the project root to Python path for module resolution
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.master_tables import SuperAdmin
from src.infra.auth.admin_auth import admin_auth
from src.infra.auth.email_service import email_service
from src.infra.database.factory import get_async_provider
from src.infra.utils.logger import get_logger

# Load environment variables from .env file
load_dotenv()

SUPER_ADMIN_EMAIL = os.getenv("SUPER_ADMIN_EMAIL")

logger = get_logger()


async def check_super_admin_exists(session: AsyncSession) -> bool:
    """Return True if a super admin already exists in the database."""
    try:
        stmt = select(SuperAdmin).where(SuperAdmin.role == "super_admin")
        result = await session.execute(stmt)
        admin = result.scalars().first()
        return admin is not None
    except Exception as e:
        logger.error(f"Error checking for existing super admin: {e}")
        # Be conservative: if we cannot check, do not proceed with creation.
        return True


async def create_super_admin_async(session: AsyncSession) -> bool:
    """
    Create the initial super admin account and send the temp password email.
    """
    if not SUPER_ADMIN_EMAIL:
        logger.error("'SUPER_ADMIN_EMAIL' environment variable is not set.")
        return False

    first_name, last_name = "Super", "Admin"
    logger.info(
        f"Creating super admin for: {first_name} {last_name} ({SUPER_ADMIN_EMAIL})"
    )

    try:
        admin, temp_password = await admin_auth.create_super_admin(
            email=SUPER_ADMIN_EMAIL,
            first_name=first_name,
            last_name=last_name,
            session=session,
        )

        logger.info(f"Successfully created super admin with ID: {admin.admin_id}")
        logger.info(f"Email: {admin.email}")
        logger.info("Sending temporary password via email...")

        email_sent = email_service.send_super_admin_creation_email(
            admin.email, admin.first_name, temp_password
        )

        if email_sent:
            logger.info("Super admin creation email sent successfully.")
        else:
            logger.warning("Failed to send super admin creation email.")
            logger.info(
                f"Please manually send the temporary password '{temp_password}' to {admin.email}"
            )
        logger.info("Super admin account created successfully.")
        return True

    except Exception as e:
        logger.error(f"Error during super admin creation: {e}")
        return False


async def main_async() -> None:
    """Main function to initialize the super admin (async)."""
    logger.info("Starting super admin initialization script...")

    provider = get_async_provider()
    try:
        async with provider.master_session() as session:
            if await check_super_admin_exists(session):
                logger.info("Super admin already exists. Skipping creation.")
                return

            logger.info("Creating new super admin account...")
            if await create_super_admin_async(session):
                logger.info("Super admin initialization completed successfully!")
            else:
                logger.error("Super admin initialization failed.")
                sys.exit(1)

    except Exception as e:
        logger.error(
            f"A critical error occurred during super admin initialization: {e}"
        )
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main_async())
