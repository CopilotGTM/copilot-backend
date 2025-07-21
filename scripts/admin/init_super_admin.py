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

# Add the project root to Python path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from dotenv import load_dotenv

load_dotenv()
from sqlmodel import Session, select
from src.infra.utils.database import init_master_db, get_session_dependency
from src.infra.auth.admin_auth import admin_auth
from src.infra.auth.email_service import email_service
from src.domain.models.admin_models import SuperAdmin
from src.infra.utils.logger import get_logger

logger = get_logger()


def check_super_admin_exists(session: Session) -> bool:
    """
    Check if a super admin already exists

    Args:
        session: Database session

    Returns:
        True if super admin exists, False otherwise
    """
    try:
        admin = session.exec(select(SuperAdmin).where(SuperAdmin.role == "super_admin")).first()
        return admin is not None
    except Exception as e:
        logger.error(f"Error checking for existing super admin: {str(e)}")
        return False


def create_super_admin(session: Session) -> bool:
    """
    Create the initial super admin account

    Args:
        session: Database session

    Returns:
        True if successful, False otherwise
    """
    try:
        # Get super admin email from environment
        super_admin_email = os.getenv("SUPER_ADMIN_EMAIL")
        if not super_admin_email:
            logger.error("SUPER_ADMIN_EMAIL environment variable is not set")
            return False

        # Parse email to get name (assuming format: firstname.lastname@domain.com)
        email_parts = super_admin_email.split("@")[0].split(".")
        if len(email_parts) >= 2:
            first_name = email_parts[0].title()
            last_name = email_parts[1].title()
        else:
            first_name = "Super"
            last_name = "Admin"

        logger.info(f"Creating super admin for: {first_name} {last_name} ({super_admin_email})")

        # Create super admin
        admin, temp_password = admin_auth.create_super_admin(super_admin_email, first_name, last_name, session)

        logger.info(f"Created super admin: {admin.admin_id}")
        logger.info(f"Email: {admin.email}")
        logger.info(f"Temporary password: {temp_password}")

        # Send email with credentials
        email_sent = email_service.send_super_admin_creation_email(admin.email, admin.first_name, temp_password)

        if email_sent:
            logger.info("Super admin creation email sent successfully")
        else:
            logger.warning("Failed to send super admin creation email")
            logger.info(f"Please manually send the temporary password '{temp_password}' to {admin.email}")

        return True

    except Exception as e:
        logger.error(f"Error creating super admin: {str(e)}")
        return False


def execute_create_super_admin():
    """
    Main function to initialize super admin
    """
    logger.info("Starting super admin initialization...")

    # Load environment variables
    load_dotenv()

    try:
        # Initialize database
        logger.info("Initializing database connection...")
        master_engine = init_master_db(create_if_missing=True)
        session = next(get_session_dependency(master_engine)())

        # Check if super admin already exists
        if check_super_admin_exists(session):
            logger.info("Super admin already exists. Skipping initialization.")
            return

        # Create super admin
        logger.info("Creating super admin account...")
        success = create_super_admin(session)

        if success:
            logger.info("Super admin initialization completed successfully!")
        else:
            logger.error("Super admin initialization failed!")
            sys.exit(1)

    except Exception as e:
        logger.error(f"Super admin initialization failed: {str(e)}")
        sys.exit(1)


# if __name__ == "__main__":
#     main()
