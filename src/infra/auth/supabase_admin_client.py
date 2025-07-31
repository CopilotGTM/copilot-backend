from typing import Dict, Any, Optional
import re
from supabase import create_client, Client
from src.infra.config.configs import supabase_config
from src.infra.utils.logger import get_logger

logger = get_logger()


class SupabaseAuthError(Exception):
    """
    Custom exception for Supabase authentication errors.
    
    Attributes:
        message: Error message
        status_code: HTTP status code
        details: Additional error details
    """
    def __init__(self, message: str, status_code: int = 500, details: Optional[Dict[str, Any]] = None):
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        super().__init__(self.message)


class SupabaseAdminClient:
    """
    Supabase client using the official Python library.
    Provides a clean interface for authentication and user management.
    """
    
    def __init__(self):
        """Initialize the Supabase client."""
        self.supabase_url = supabase_config.SUPABASE_URL
        self.supabase_anon_key = supabase_config.SUPABASE_ANON_KEY
        self.supabase_service_role_key = supabase_config.SUPABASE_SERVICE_ROLE_KEY
        self.jwt_secret = supabase_config.SUPABASE_JWT_SECRET
        
        # Validate required configuration
        self._validate_config()
        
        # Create client with anon key for user operations
        self.client: Client = create_client(self.supabase_url, self.supabase_anon_key)
        
        # Create admin client with service role for admin operations
        self.admin_client: Client = create_client(self.supabase_url, self.supabase_service_role_key)
        
        logger.info("Supabase client initialized")
    
    def _validate_config(self) -> None:
        """
        Validate that all required Supabase configuration is present.
        
        Raises:
            SupabaseAuthError: If any required configuration is missing
        """
        missing_configs = []
        
        if not self.supabase_url:
            missing_configs.append("SUPABASE_URL")
        if not self.supabase_anon_key:
            missing_configs.append("SUPABASE_ANON_KEY")
        if not self.supabase_service_role_key:
            missing_configs.append("SUPABASE_SERVICE_ROLE_KEY")
        if not self.jwt_secret:
            missing_configs.append("SUPABASE_JWT_SECRET")
            
        if missing_configs:
            raise SupabaseAuthError(
                f"Missing required Supabase configuration: {', '.join(missing_configs)}",
                status_code=500
            )

    def _validate_email(self, email: str) -> bool:
        """
        Validate email format using regex pattern.
        
        Args:
            email: Email address to validate
            
        Returns:
            True if email format is valid, False otherwise
        """
        if not email or not isinstance(email, str):
            return False
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        return bool(re.match(email_pattern, email))

    def _validate_password(self, password: str) -> bool:
        """
        Validate password strength requirements.
        
        Args:
            password: Password to validate
            
        Returns:
            True if password meets requirements, False otherwise
        """
        if not password or not isinstance(password, str):
            return False
        # Minimum 6 characters
        if len(password) < 6:
            return False
        return True

    def _validate_user_id(self, user_id: str) -> bool:
        """
        Validate user ID format (UUID).
        
        Args:
            user_id: User ID to validate
            
        Returns:
            True if user ID format is valid, False otherwise
        """
        if not user_id or not isinstance(user_id, str):
            return False
        # UUID pattern
        uuid_pattern = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        return bool(re.match(uuid_pattern, user_id.lower()))

    def _validate_token(self, token: str) -> bool:
        """
        Validate JWT token format.
        
        Args:
            token: JWT token to validate
            
        Returns:
            True if token format is valid, False otherwise
        """
        if not token or not isinstance(token, str):
            return False
        # Basic JWT token validation (3 parts separated by dots)
        parts = token.split('.')
        return len(parts) == 3
    
    def _validate_hashed_token(self, hashed_token: str) -> bool:
        """
        Validate hashed token format (for password reset).
        
        Args:
            hashed_token: Hashed token to validate
            
        Returns:
            True if hashed token format is valid, False otherwise
        """
        if not hashed_token or not isinstance(hashed_token, str):
            return False
        # Hashed tokens are typically base64 encoded strings
        # They don't have the JWT structure (3 parts with dots)
        # Just check if it's a non-empty string
        return len(hashed_token.strip()) > 0
    
    def sign_in_with_password(self, email: str, password: str) -> Dict[str, Any]:
        """
        Sign in user with email and password.
        
        Args:
            email: User's email
            password: User's password
            
        Returns:
            Supabase response with user data and tokens
        """
        try:
            # Input validation
            if not self._validate_email(email):
                raise SupabaseAuthError("Invalid email format", status_code=400)
            if not self._validate_password(password):
                raise SupabaseAuthError("Invalid password format", status_code=400)
                
            response = self.client.auth.sign_in_with_password({
                "email": email,
                "password": password
            })
            logger.info(f"User signed in successfully: {email}")
            return response.model_dump()
        except Exception as e:
            logger.error(f"Sign in failed for {email}: {str(e)}")
            raise SupabaseAuthError(f"Sign in failed: {str(e)}", status_code=401)
    
    def generate_password_reset_link(self, email: str) -> str:
        """
        Generate a password reset link and extract the token parameter.
        
        Args:
            email: User's email address
            
        Returns:
            The token parameter value from the reset link
            
        Raises:
            SupabaseAuthError: If link generation fails
        """
        try:
            # Input validation
            if not self._validate_email(email):
                raise SupabaseAuthError("Invalid email format", status_code=400)
            
            # Get APP_URL from environment
            import os
            app_url = os.getenv("APP_URL", "http://localhost:3000")
            redirect_url = f"{app_url}/reset-password"
            
            # Generate recovery link using admin client (doesn't send email)
            response = self.admin_client.auth.admin.generate_link({
                "type": "recovery",
                "email": email
            })
            
            # The response should contain the recovery link
            response_data = response.model_dump()

            # print(response_data)

            hashed_token = response_data.get("properties", {}).get("hashed_token")
            
            if not hashed_token:
                raise SupabaseAuthError("Failed to generate hash token", status_code=500)
                
            logger.info(f"Password reset link generated for user: {email}")
            return f'{redirect_url}?token_hash={hashed_token}'
            
        except Exception as e:
            logger.error(f"Failed to generate password reset link for {email}: {str(e)}")
            raise SupabaseAuthError(f"Failed to generate password reset link: {str(e)}", status_code=500)

    def update_user_password(self, user_id: str, new_password: str) -> Dict[str, Any]:
        """
        Update user's password using admin client.
        
        Args:
            user_id: Supabase user ID
            new_password: New password
            
        Returns:
            Updated user data
        """
        try:
            # Input validation
            if not self._validate_user_id(user_id):
                raise SupabaseAuthError("Invalid user ID format", status_code=400)
            if not self._validate_password(new_password):
                raise SupabaseAuthError("Invalid password format", status_code=400)
                
            response = self.admin_client.auth.admin.update_user_by_id(
                user_id,
                {"password": new_password}
            )
            logger.info(f"Password updated for user: {user_id}")
            return response.model_dump()
        except Exception as e:
            logger.error(f"Password update failed for user {user_id}: {str(e)}")
            raise SupabaseAuthError(f"Password update failed: {str(e)}", status_code=500)
    
    def get_user_by_id(self, user_id: str) -> Dict[str, Any]:
        """
        Get user by ID using admin client.
        
        Args:
            user_id: Supabase user ID
            
        Returns:
            User data
        """
        try:
            # Input validation
            if not self._validate_user_id(user_id):
                raise SupabaseAuthError("Invalid user ID format", status_code=400)
                
            response = self.admin_client.auth.admin.get_user_by_id(user_id)
            logger.info(f"Retrieved user: {user_id}")
            return response.model_dump()
        except Exception as e:
            logger.error(f"Failed to get user {user_id}: {str(e)}")
            raise SupabaseAuthError(f"Failed to get user: {str(e)}", status_code=500)
    
    def update_user_metadata(self, user_id: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update user metadata using admin client.
        
        Args:
            user_id: Supabase user ID
            metadata: User metadata to update
            
        Returns:
            Updated user data
        """
        try:
            # Input validation
            if not self._validate_user_id(user_id):
                raise SupabaseAuthError("Invalid user ID format", status_code=400)
                
            response = self.admin_client.auth.admin.update_user_by_id(
                user_id,
                {"user_metadata": metadata}
            )
            logger.info(f"Metadata updated for user: {user_id}")
            return response.model_dump()
        except Exception as e:
            logger.error(f"Metadata update failed for user {user_id}: {str(e)}")
            raise SupabaseAuthError(f"Metadata update failed: {str(e)}", status_code=500)

    def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """
        Get user by email using admin client.
        
        Args:
            email: User's email
            
        Returns:
            User data if found, None otherwise
        """
        try:
            if not self._validate_email(email):
                raise SupabaseAuthError("Invalid email format", status_code=400)
            
            # This is a workaround since supabase-py doesn't have a direct get_user_by_email
            users_response = self.admin_client.auth.admin.list_users()
            
            # The list_users() method in the version of supabase-py being used
            # appears to return a list of users directly, not a response object.
            for user in users_response:
                if user.email == email:
                    logger.info(f"User found by email: {email}")
                    return user.model_dump()
            
            logger.info(f"User not found by email: {email}")
            return None
        except Exception as e:
            # If the user is not found, Supabase might raise an error.
            # We'll log it and return None, as this is an expected case.
            if "User not found" in str(e):
                 logger.info(f"User not found by email: {email}")
                 return None
            logger.error(f"Failed to get user by email {email}: {str(e)}")
            raise SupabaseAuthError(f"Failed to get user by email: {str(e)}", status_code=500)
    
    def sign_out_user(self, user_id: str, global_signout: bool = False) -> bool:
        """
        Sign out user from all sessions.
        
        Args:
            user_id: Supabase user ID
            global_signout: If True, sign out from all sessions globally. If False, sign out from current session only.
            
        Returns:
            True if sign out was successful
            
        Raises:
            SupabaseAuthError: If sign out fails
        """
        try:
            # Input validation
            if not self._validate_user_id(user_id):
                raise SupabaseAuthError("Invalid user ID format", status_code=400)
            
            if global_signout:
                # Sign out from all sessions globally
                self.client.auth.sign_out({
                    "scope": "global"
                })
                logger.info(f"User {user_id} signed out globally from all sessions")
            else:
                # Sign out from current session only
                self.client.auth.sign_out({
                    "scope": "local"
                })
                logger.info(f"User {user_id} signed out from current session")
            
            return True
            
        except Exception as e:
            logger.error(f"Sign out failed for user {user_id}: {str(e)}")
            raise SupabaseAuthError(f"Sign out failed: {str(e)}", status_code=500)
    
    def create_user(self, email: str, password: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Create a new user using admin client.
        
        Args:
            email: User's email
            password: User's password
            metadata: Optional user metadata
            
        Returns:
            Created user data
        """
        try:
            # Input validation
            if not self._validate_email(email):
                raise SupabaseAuthError("Invalid email format", status_code=400)
            if not self._validate_password(password):
                raise SupabaseAuthError("Invalid password format", status_code=400)
                
            user_data = {
                "email": email,
                "password": password,
                "email_confirm": True
            }
            if metadata:
                user_data["user_metadata"] = metadata
                
            response = self.admin_client.auth.admin.create_user(user_data)
            logger.info(f"User created: {email}")
            return response.model_dump()
        except Exception as e:
            logger.error(f"User creation failed for {email}: {str(e)}")
            raise SupabaseAuthError(f"User creation failed: {str(e)}", status_code=500)
    
    def verify_token(self, token: str) -> Dict[str, Any]:
        """
        Verify a JWT token and return user information.
        
        Args:
            token: JWT token to verify
            
        Returns:
            User information from token
        """
        try:
            # Input validation
            if not self._validate_token(token):
                raise SupabaseAuthError("Invalid token format", status_code=400)
                
            response = self.client.auth.get_user(token)
            logger.info("Token verified successfully")
            return response.model_dump()
        except Exception as e:
            logger.error(f"Token verification failed: {str(e)}")
            raise SupabaseAuthError(f"Token verification failed: {str(e)}", status_code=401)
    
    def refresh_access_token(self, refresh_token: str) -> Dict[str, Any]:
        """
        Refresh access token using refresh token.
        
        Args:
            refresh_token: Refresh token
            
        Returns:
            New session data with access and refresh tokens
        """
        try:
            # Input validation
            if not self._validate_token(refresh_token):
                raise SupabaseAuthError("Invalid refresh token format", status_code=400)
                
            response = self.client.auth.refresh_session(refresh_token)
            logger.info("Access token refreshed successfully")
            return response.model_dump()
        except Exception as e:
            logger.error(f"Token refresh failed: {str(e)}")
            raise SupabaseAuthError(f"Token refresh failed: {str(e)}", status_code=401)
    
    def reset_password_with_token(self, token_hash: str, new_password: str) -> bool:
        """
        Reset password using a recovery token_hash.
        
        Args:
            token_hash: Recovery token hash from password reset link
            new_password: New password
            
        Returns:
            True if password reset was successful
            
        Raises:
            SupabaseAuthError: If password reset fails
        """
        try:
            # Input validation
            if not self._validate_hashed_token(token_hash):
                raise SupabaseAuthError("Invalid hashed token format", status_code=400)
            if not self._validate_password(new_password):
                raise SupabaseAuthError("Invalid password format", status_code=400)
            
            # Use the recovery token_hash to verify and reset password
            response = self.client.auth.verify_otp({
                "token_hash": token_hash,
                "type": "recovery",
            })
            
            # If verification is successful, update the password
            if response:
                # Get user from the response
                user_data = response.model_dump()
                user_id = user_data.get("user", {}).get("id")
                
                if user_id:
                    # Update password using admin client
                    self.admin_client.auth.admin.update_user_by_id(
                        user_id,
                        {"password": new_password}
                    )
                    
                    logger.info(f"Password reset successful for user ID: {user_id}")
                    return user_id
                else:
                    raise SupabaseAuthError("Failed to get user ID from verification", status_code=500)
            else:
                raise SupabaseAuthError("Token verification failed", status_code=400)
            
        except Exception as e:
            logger.error(f"Password reset with token failed: {str(e)}")
            raise SupabaseAuthError(f"Password reset failed: {str(e)}", status_code=500)

    def get_user_from_token(self, token: str, session) -> Optional[Any]:
        """
        Get user from token and database session.
        
        Args:
            token: JWT token
            session: Database session
            
        Returns:
            User object if found, None otherwise
        """
        try:
            # Verify token with Supabase
            user_data = self.verify_token(token)
            
            # Extract user ID from token
            supabase_user_id = user_data.get("user", {}).get("id")
            if not supabase_user_id:
                return None
            
            # Find user in database by supabase_user_id
            from sqlmodel import select
            from src.domain.models.master_db_models import User
            
            statement = select(User).where(User.supabase_user_id == supabase_user_id)
            user = session.exec(statement).first()
            
            return user
            
        except Exception as e:
            logger.error(f"Failed to get user from token: {str(e)}")
            return None


# Create global instance
supabase_admin_client = SupabaseAdminClient() 