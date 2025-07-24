import jwt
import requests
from typing import Dict, Any, Optional
from fastapi import HTTPException, status
from sqlmodel import Session, select
from uuid import UUID

from src.domain.models.db_models import User
from src.infra.config.configs import supabase_config
from src.infra.utils.logger import get_logger

logger = get_logger()


class SupabaseAuth:
    """Supabase authentication utility"""

    def __init__(self):
        self.supabase_url = supabase_config.SUPABASE_URL
        self.supabase_anon_key = supabase_config.SUPABASE_ANON_KEY
        self.supabase_service_role_key = supabase_config.SUPABASE_SERVICE_ROLE_KEY
        self.jwt_secret = supabase_config.SUPABASE_JWT_SECRET

    def verify_email_password(self, email: str, password: str) -> Dict[str, Any]:
        """
        Verify email and password with Supabase Auth (Password Grant).
        Returns the Supabase response (should contain access_token on success).
        """
        try:
            url = f"{self.supabase_url}/auth/v1/token?grant_type=password"
            headers = {"apikey": self.supabase_anon_key, "Content-Type": "application/json"}
            payload = {"email": email, "password": password}
            response = requests.post(url, headers=headers, json=payload)
            if response.status_code == 200:
                logger.info("Supabase password verification succeeded for %s", email)
                return response.json()
            else:
                logger.warning("Supabase password verification failed for %s: %s", email, response.text)
                return {}
        except Exception as e:
            logger.error("Error verifying password with Supabase: %s", str(e))
            return {}

    def refresh_access_token(self, refresh_token: str) -> Dict[str, Any]:
        """
        Refresh the access token using a refresh token.
        Returns the new session data (including new access_token and refresh_token).
        """
        try:
            url = f"{self.supabase_url}/auth/v1/token?grant_type=refresh_token"
            headers = {"apikey": self.supabase_anon_key, "Content-Type": "application/json"}
            payload = {"refresh_token": refresh_token}
            response = requests.post(url, headers=headers, json=payload)
            if response.status_code == 200:
                logger.info("Supabase token refresh succeeded.")
                return response.json()
            else:
                logger.warning("Supabase token refresh failed: %s", response.text)
                return {}
        except Exception as e:
            logger.error("Error refreshing token with Supabase: %s", str(e))
            return {}

    def verify_token(self, token: str) -> Dict[str, Any]:
        """
        Verify a Supabase JWT token and return the payload.

        Args:
            token: The JWT token from Supabase

        Returns:
            Dict containing the token payload with user information

        Raises:
            HTTPException: If token is invalid or expired
        """
        try:
            # Decode the JWT token
            payload = jwt.decode(token, self.jwt_secret, algorithms=["HS256"], audience="authenticated")

            logger.debug("Token verified successfully")
            return payload

        except jwt.ExpiredSignatureError:
            logger.warning("Token has expired")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has expired")
        except jwt.InvalidTokenError as e:
            logger.error("Invalid token: %s", str(e))
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
        except Exception as e:
            logger.error("Token verification failed: %s", str(e))
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Token verification failed")

    def get_user_from_token(self, token: str, session: Session) -> Optional[User]:
        """
        Get user from database using Supabase token.

        Args:
            token: The Supabase JWT token
            session: Database session

        Returns:
            User object if found, None otherwise
        """
        try:
            # Verify the token
            payload = self.verify_token(token)

            # Extract user ID from token
            supabase_user_id = payload.get("sub")  # Supabase user ID
            if not supabase_user_id:
                logger.warning("No user ID found in token")
                return None

            # Find user in database by Supabase user ID
            stmt = select(User).where(User.supabase_user_id == supabase_user_id)
            user = session.exec(stmt).one_or_none()

            if not user:
                logger.warning("User not found in database for supabase_user_id: %s", supabase_user_id)
                return None

            logger.info("User found: %s", user.user_id)
            return user

        except Exception as e:
            logger.error("Failed to get user from token: %s", str(e))
            return None

    def create_user_from_supabase(self, supabase_user_data: Dict[str, Any], session: Session) -> Optional[User]:
        """
        Create a new user in the database from Supabase user data.

        Args:
            supabase_user_data: User data from Supabase
            session: Database session

        Returns:
            Created User object or None if creation failed
        """
        try:
            # Extract user information from Supabase data
            supabase_user_id = supabase_user_data.get("id")
            email = supabase_user_data.get("email")

            if not supabase_user_id or not email:
                logger.error("Missing required user data from Supabase")
                return None

            # Check if user already exists
            existing_user = session.exec(select(User).where(User.supabase_user_id == supabase_user_id)).one_or_none()

            if existing_user:
                logger.info("User already exists: %s", existing_user.user_id)
                return existing_user

            # Create new user (you'll need to get organisation_id from somewhere)
            # This is a placeholder - you'll need to implement the logic to determine
            # which organisation the user belongs to
            new_user = User(
                supabase_user_id=supabase_user_id,
                email=email,
                first_name=supabase_user_data.get("user_metadata", {}).get("first_name", ""),
                last_name=supabase_user_data.get("user_metadata", {}).get("last_name", ""),
                organisation_role="user",  # Default role
                # organisation_id needs to be set based on your business logic
                # You might get this from user metadata or a separate API call
            )

            session.add(new_user)
            session.commit()
            session.refresh(new_user)

            logger.info("Created new user: %s", new_user.user_id)
            return new_user

        except Exception as e:
            logger.error("Failed to create user from Supabase data: %s", str(e))
            session.rollback()
            return None

    def get_supabase_user_info(self, token: str) -> Optional[Dict[str, Any]]:
        """
        Get user information from Supabase using the access token.

        Args:
            token: Supabase access token

        Returns:
            User information from Supabase or None if failed
        """
        try:
            headers = {"Authorization": f"Bearer {token}", "apikey": self.supabase_anon_key}

            response = requests.get(f"{self.supabase_url}/auth/v1/user", headers=headers)

            if response.status_code == 200:
                return response.json()
            else:
                logger.error("Failed to get user info from Supabase: %s", response.text)
                return None

        except Exception as e:
            logger.error(f"Error getting user info from Supabase: {str(e)}")
            return None

    def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        Get user information from Supabase by user ID using the service role.

        Args:
            user_id: Supabase user ID

        Returns:
            User information from Supabase or None if failed
        """
        try:
            headers = {
                "Authorization": f"Bearer {self.supabase_service_role_key}",
                "apikey": self.supabase_service_role_key,
            }

            response = requests.get(f"{self.supabase_url}/auth/v1/admin/users/{user_id}", headers=headers)

            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Failed to get user info from Supabase for user_id {user_id}: {response.text}")
                return None

        except Exception as e:
            logger.error(f"Error getting user info from Supabase for user_id {user_id}: {str(e)}")
            return None

    def update_user_password(self, user_id: str, new_password: str) -> bool:
        """
        Update a user's password in Supabase using the service role.

        Args:
            user_id: Supabase user ID
            new_password: The new password

        Returns:
            True if successful, False otherwise
        """
        try:
            headers = {
                "Authorization": f"Bearer {self.supabase_service_role_key}",
                "apikey": self.supabase_service_role_key,
            }

            payload = {"password": new_password}

            response = requests.put(f"{self.supabase_url}/auth/v1/admin/users/{user_id}", headers=headers, json=payload)

            if response.status_code == 200:
                logger.info(f"Successfully updated password for user_id {user_id}")
                return True
            else:
                logger.error(f"Failed to update password for user_id {user_id}: {response.text}")
                return False

        except Exception as e:
            logger.error(f"Error updating password for user_id {user_id}: {str(e)}")
            return False

    def sign_out_user_globally(self, user_id: str) -> bool:
        """
        Sign out a user from all active sessions using the service role.

        Args:
            user_id: Supabase user ID

        Returns:
            True if successful, False otherwise
        """
        try:
            headers = {
                "Authorization": f"Bearer {self.supabase_service_role_key}",
                "apikey": self.supabase_service_role_key,
            }

            response = requests.post(f"{self.supabase_url}/auth/v1/admin/users/{user_id}/logout", headers=headers)

            if response.status_code == 200:
                logger.info(f"Successfully signed out user_id {user_id} from all sessions.")
                return True
            else:
                logger.error(f"Failed to sign out user_id {user_id}: {response.text}")
                return False

        except Exception as e:
            logger.error(f"Error signing out user_id {user_id}: {str(e)}")
            return False


# Create a global instance
supabase_auth = SupabaseAuth()
