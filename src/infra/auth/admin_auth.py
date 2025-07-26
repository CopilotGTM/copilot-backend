import os
import secrets
import string
from datetime import datetime
from typing import Optional, Dict, Any
from uuid import UUID
from fastapi import HTTPException, status, Depends, Request
from sqlmodel import Session, select
import requests

from src.domain.models.master_db_models import SuperAdmin, AdminStatus, AdminRole
from src.infra.utils.database import get_master_session
from src.infra.utils.logger import get_logger
from src.infra.auth.email_service import email_service
from src.infra.auth.supabase_auth import supabase_auth
from src.infra.config.configs import supabase_config

logger = get_logger()


class AdminAuth:
    """Admin authentication and management service using Supabase"""
 
    def __init__(self, allow_pending_setup: bool = False):
        self.allow_pending_setup = allow_pending_setup
        self.app_url = os.getenv("APP_URL", "http://localhost:3000")
        self.supabase_url = supabase_config.SUPABASE_URL
        self.supabase_service_role_key = supabase_config.SUPABASE_SERVICE_ROLE_KEY
 
    async def __call__(self, request: Request, session: Session = Depends(get_master_session)) -> SuperAdmin:
        """
        FastAPI dependency to get current authenticated admin from Supabase token

        Args:
            request: The request object
            session: Database session

        Returns:
            SuperAdmin object

        Raises:
            HTTPException: If not authenticated
        """
        logger.info("Authenticating admin user")
        authorization: str = request.headers.get("Authorization")
        if not authorization:
            logger.warning("Authorization header missing")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authorization header missing")

        token_type, _, token = authorization.partition(" ")
        if token_type.lower() != "bearer" or not token:
            logger.warning("Invalid token format")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token format")

        try:
            logger.info("Getting user info from Supabase")
            user_response = supabase_auth.get_supabase_user_info(token)
            if not user_response:
                logger.warning("Invalid or expired token from Supabase")
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

            supabase_user_id = user_response.get("id")
            logger.info(f"Getting admin by supabase_user_id: {supabase_user_id}")
            admin = self.get_admin_by_supabase_id(supabase_user_id, session)

            if not admin:
                logger.warning(f"Admin not found for supabase_user_id: {supabase_user_id}")
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin not found for the given token"
                )
 
            if admin.status == AdminStatus.PENDING_SETUP:
                logger.warning(f"Admin {admin.email} attempted to access a protected route with PENDING_SETUP status")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="User is pending setup. Password change is required before accessing this resource.",
                )
 
            logger.info(f"Admin authenticated successfully: {admin.email}")
            return admin
        except Exception as e:
            logger.error(f"Authentication error: {str(e)}")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Could not validate credentials")

    def generate_temp_password(self, length: int = 12) -> str:
        """
        Generate a secure temporary password

        Args:
            length: Length of the password

        Returns:
            Generated temporary password
        """
        logger.info(f"Generating temporary password of length {length}")
        # Use a mix of letters, digits, and symbols
        characters = string.ascii_letters + string.digits + "!@#$%^&*"
        password = "".join(secrets.choice(characters) for _ in range(length))
        logger.info("Temporary password generated")
        return password

    def create_supabase_user(self, email: str, password: str, first_name: str, last_name: str) -> Optional[str]:
        """
        Create user in Supabase auth using service role

        Args:
            email: User email
            password: User password
            first_name: User first name
            last_name: User last name

        Returns:
            Supabase user ID if successful, None otherwise
        """
        logger.info(f"Creating Supabase user for email: {email}")
        try:
            headers = {
                "apikey": self.supabase_service_role_key,
                "Authorization": f"Bearer {self.supabase_service_role_key}",
                "Content-Type": "application/json",
            }

            user_data = {
                "email": email,
                "password": password,
                "email_confirm": True,  # Auto-confirm email
                "user_metadata": {
                    "first_name": first_name,
                    "last_name": last_name,
                    "role": "super_admin",
                    "requires_password_change": True,
                },
            }

            logger.info("Sending request to create Supabase user")
            response = requests.post(f"{self.supabase_url}/auth/v1/admin/users", headers=headers, json=user_data)

            if response.status_code == 200:
                user_info = response.json()
                logger.info(f"Created Supabase user: {user_info.get('id')}")
                return user_info.get("id")
            else:
                logger.error(f"Failed to create Supabase user: {response.text}")
                return None

        except Exception as e:
            logger.error(f"Error creating Supabase user: {str(e)}")
            return None

    def update_supabase_user_metadata(self, supabase_user_id: str, metadata: Dict[str, Any]) -> bool:
        """
        Update user metadata in Supabase

        Args:
            supabase_user_id: Supabase user ID
            metadata: Metadata to update

        Returns:
            True if successful, False otherwise
        """
        logger.info(f"Updating Supabase user metadata for user: {supabase_user_id}")
        try:
            headers = {
                "apikey": self.supabase_service_role_key,
                "Authorization": f"Bearer {self.supabase_service_role_key}",
                "Content-Type": "application/json",
            }

            logger.info("Sending request to update Supabase user metadata")
            response = requests.put(
                f"{self.supabase_url}/auth/v1/admin/users/{supabase_user_id}",
                headers=headers,
                json={"user_metadata": metadata},
            )

            if response.status_code == 200:
                logger.info(f"Updated Supabase user metadata: {supabase_user_id}")
                return True
            else:
                logger.error(f"Failed to update Supabase user metadata: {response.text}")
                return False

        except Exception as e:
            logger.error(f"Error updating Supabase user metadata: {str(e)}")
            return False

    def create_super_admin(
        self, email: str, first_name: str, last_name: str, session: Session
    ) -> tuple[SuperAdmin, str]:
        """
        Create a new super admin account

        Args:
            email: Admin email
            first_name: Admin first name
            last_name: Admin last name
            session: Database session

        Returns:
            Tuple of (SuperAdmin object, temporary password)

        Raises:
            HTTPException: If admin already exists
        """
        logger.info(f"Creating super admin for email: {email}")
        # Check if admin already exists
        logger.info(f"Checking for existing admin with email: {email}")
        existing_admin = session.exec(select(SuperAdmin).where(SuperAdmin.email == email)).one_or_none()

        if existing_admin:
            logger.warning(f"Admin with email {email} already exists")
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Admin with this email already exists")

        # Generate temporary password
        logger.info("Generating temporary password for new admin")
        temp_password = self.generate_temp_password()

        # Create user in Supabase auth
        logger.info("Creating user in Supabase for new admin")
        supabase_user_id = self.create_supabase_user(email, temp_password, first_name, last_name)

        if not supabase_user_id:
            logger.error("Failed to create user in Supabase")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create user in authentication system",
            )

        # Create admin in database
        logger.info(f"Creating admin record in database for supabase_user_id: {supabase_user_id}")
        admin = SuperAdmin(
            supabase_user_id=supabase_user_id,
            email=email,
            first_name=first_name,
            last_name=last_name,
            role=AdminRole.SUPER_ADMIN,
            status=AdminStatus.PENDING_SETUP,
        )

        session.add(admin)
        session.commit()
        session.refresh(admin)

        logger.info(f"Created super admin: {admin.admin_id}")
        return admin, temp_password

    def get_admin_by_supabase_id(self, supabase_user_id: str, session: Session) -> Optional[SuperAdmin]:
        """
        Get admin by Supabase user ID

        Args:
            supabase_user_id: Supabase user ID
            session: Database session

        Returns:
            SuperAdmin object if found, None otherwise
        """
        logger.info(f"Getting admin by Supabase ID: {supabase_user_id}")
        admin = session.exec(select(SuperAdmin).where(SuperAdmin.supabase_user_id == supabase_user_id)).one_or_none()
        if admin:
            logger.info(f"Found admin: {admin.admin_id}")
        else:
            logger.info("Admin not found")
        return admin

    def get_admin_by_email(self, email: str, session: Session) -> Optional[SuperAdmin]:
        """
        Get admin by email

        Args:
            email: Admin email
            session: Database session

        Returns:
            SuperAdmin object if found, None otherwise
        """
        logger.info(f"Getting admin by email: {email}")
        admin = session.exec(select(SuperAdmin).where(SuperAdmin.email == email)).one_or_none()
        if admin:
            logger.info(f"Found admin: {admin.admin_id}")
        else:
            logger.info("Admin not found")
        return admin

    def update_admin_login_time(self, admin_id: UUID, session: Session) -> bool:
        """
        Update admin's last login time

        Args:
            admin_id: Admin ID
            session: Database session

        Returns:
            True if successful, False otherwise
        """
        logger.info(f"Updating last login time for admin: {admin_id}")
        try:
            admin = session.exec(select(SuperAdmin).where(SuperAdmin.admin_id == admin_id)).one_or_none()

            if admin:
                admin.last_login_at = datetime.utcnow()
                session.commit()
                logger.info(f"Updated last login time for admin: {admin_id}")
                return True

            logger.warning(f"Admin not found for login time update: {admin_id}")
            return False
        except Exception as e:
            logger.error(f"Error updating admin login time: {str(e)}")
            return False

    def update_admin_status(self, admin_id: UUID, status: AdminStatus, session: Session) -> bool:
        """
        Update admin status

        Args:
            admin_id: Admin ID
            status: New status
            session: Database session

        Returns:
            True if successful, False otherwise
        """
        logger.info(f"Updating status for admin: {admin_id} to {status.value}")
        try:
            admin = session.exec(select(SuperAdmin).where(SuperAdmin.admin_id == admin_id)).one_or_none()

            if admin:
                admin.status = status
                session.commit()
                logger.info(f"Updated status for admin: {admin_id}")
                return True

            logger.warning(f"Admin not found for status update: {admin_id}")
            return False
        except Exception as e:
            logger.error(f"Error updating admin status: {str(e)}")
            return False

    def mark_password_changed(self, supabase_user_id: str, session: Session) -> bool:
        """
        Mark that admin has changed their password for the first time

        Args:
            supabase_user_id: Supabase user ID
            session: Database session

        Returns:
            True if successful, False otherwise
        """
        logger.info(f"Marking password changed for supabase_user_id: {supabase_user_id}")
        try:
            # Update Supabase user metadata
            logger.info("Updating Supabase user metadata for password change")
            metadata_update = {"requires_password_change": False, "password_changed_at": datetime.utcnow().isoformat()}

            supabase_updated = self.update_supabase_user_metadata(supabase_user_id, metadata_update)

            if not supabase_updated:
                logger.warning("Failed to update Supabase user metadata")

            # Update admin status in database
            logger.info("Updating admin status to ACTIVE")
            admin = self.get_admin_by_supabase_id(supabase_user_id, session)
            if admin:
                admin.status = AdminStatus.ACTIVE
                session.commit()
                logger.info(f"Marked password changed for admin: {admin.admin_id}")
                return True

            logger.warning(f"Admin not found for password change marking: {supabase_user_id}")
            return False
        except Exception as e:
            logger.error(f"Error marking password changed: {str(e)}")
            return False

    def check_requires_password_change(self, supabase_user_id: str) -> bool:
        """
        Check if user requires password change

        Args:
            supabase_user_id: Supabase user ID

        Returns:
            True if password change required, False otherwise
        """
        logger.info(f"Checking if password change is required for supabase_user_id: {supabase_user_id}")
        try:
            headers = {
                "apikey": self.supabase_service_role_key,
                "Authorization": f"Bearer {self.supabase_service_role_key}",
            }

            logger.info("Getting user info from Supabase for password change check")
            response = requests.get(f"{self.supabase_url}/auth/v1/admin/users/{supabase_user_id}", headers=headers)

            if response.status_code == 200:
                user_info = response.json()
                user_metadata = user_info.get("user_metadata", {})
                requires_change = user_metadata.get("requires_password_change", False)
                logger.info(f"Password change required: {requires_change}")
                return requires_change
            else:
                logger.error(f"Failed to get user info: {response.text}")
                return False

        except Exception as e:
            logger.error(f"Error checking password change requirement: {str(e)}")
            return False


# Create global instance
admin_auth = AdminAuth()
admin_auth_allow_pending = AdminAuth(allow_pending_setup=True)
