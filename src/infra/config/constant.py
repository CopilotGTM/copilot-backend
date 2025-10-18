import os

CONFIG_MAP = {
    "local": {
        "redis_client": {
            "host": os.getenv("REDIS_HOST", "localhost"),
            "port": int(os.getenv("REDIS_PORT", 6379)),
            "password": os.getenv("REDIS_PASSWORD", None),
        },
    },
    "dev": {
        "redis_client": {
            "host": os.getenv("REDIS_HOST", "localhost"),
            "port": int(os.getenv("REDIS_PORT", 6379)),
            "password": os.getenv("REDIS_PASSWORD", None),
        },
    },
    "prod": {
        "redis_client": {
            "host": os.getenv("REDIS_HOST", "localhost"),
            "port": int(os.getenv("REDIS_PORT", 6379)),
            "password": os.getenv("REDIS_PASSWORD", None),
        },
    },
}

GRAPH_API_URL = "https://graph.microsoft.com/v1.0"
APP_NAME = "CopilotGTM"


class GeneralErrors:
    """Generic, reusable error messages for consistent API responses."""

    UNEXPECTED_ERROR = "An unexpected error occurred"
    BAD_REQUEST = "Invalid request"
    UNAUTHORIZED = "Unauthorized"
    USER_NOT_FOUND = "User not found"
    FORBIDDEN = "Forbidden"
    NOT_FOUND = "Not found"
    CONFLICT = "Conflict"
    ALREADY_EXISTS = "Already exists"
    VALIDATION_FAILED = "Validation failed"
    RATE_LIMITED = "Too many requests"
    SERVICE_UNAVAILABLE = "Service temporarily unavailable"
    TIMEOUT = "Request timed out"
    DEPENDENCY_ERROR = "Upstream service error"


class AdminAuthErrors:
    USER_NOT_FOUND = GeneralErrors.USER_NOT_FOUND
    INVALID_CREDENTIALS = "Invalid credentials"
    LOGIN_FAILED = "Login failed"
    RECOVERY_TOKEN_REQUIRED = "Recovery token and new password are required."
    PASSWORD_MISMATCH = "New password and confirm password do not match"
    INVALID_RECOVERY_TOKEN = "Invalid or expired recovery token"
    UNAUTHORIZED = GeneralErrors.UNAUTHORIZED
    PASSWORD_RESET_FAILED = "Password reset failed"
    REFRESH_TOKEN_INVALID = "Invalid or expired refresh token. Please log in again."
    REFRESH_TOKEN_FAILED = "Token refresh failed."
    FORGOT_PASSWORD_FAILED = "Forgot password request failed"
    EMAIL_REQUIRED_FOR_LOGOUT = "Email is required for global logout"
    LOGOUT_FAILED = "An unexpected error occurred during logout."
    INCORRECT_CURRENT_PASSWORD = "Incorrect current password"
    PASSWORD_CHANGE_FAILED = "Password change failed"


class AdminOrganizationErrors:
    SUPER_ADMIN_ACCESS_REQUIRED = "Super admin access required"
    ADMIN_ACCESS_REQUIRED = "Admin access required"
    ORGANIZATION_NOT_FOUND = "Organization not found"
    NO_UPDATE_DATA = "No update data provided"


class AdminUserErrors:
    ADMIN_ACCESS_REQUIRED = "Admin access required"
    CREATE_USER_MISSING_FIELDS = "Email, first name, and last name are required."
    CREATE_USER_MISSING_ORG_ID = "Organisation ID is required."
    CREATE_USER_INVALID_ROLE = "Invalid user role. Must be 'MEMBER' or 'ADMIN'."
    USER_NOT_FOUND = "User not found"
    USER_NOT_APPROVED = "User is not approved"
    ORGANISATION_NOT_FOUND = "Organisation not found"
    USER_ALREADY_EXISTS = "User with this email already exists"
    NO_UPDATE_DATA_PROVIDED = "No update data provided"


class UserAuthErrors:
    USER_NOT_FOUND = GeneralErrors.USER_NOT_FOUND
    INVALID_CREDENTIALS = "Invalid credentials"
    LOGIN_FAILED = "login failed"
    PASSWORD_CHANGE_TOKEN_FAILED = "Failed to generate password change token"
    RECOVERY_TOKEN_REQUIRED = "Recovery token_hash is required."
    NEW_PASSWORD_REQUIRED = "New password is required."
    PASSWORD_MISMATCH = "New password and confirm password do not match"
    INVALID_ACTION = "Invalid action. Must be 'force_change' or 'forgot'."
    INVALID_RECOVERY_TOKEN = "Invalid or expired recovery token"
    UNAUTHORIZED = GeneralErrors.UNAUTHORIZED
    UPDATE_METADATA_FAILED = "Failed to update user metadata"
    UPDATE_USER_DATA_FAILED = "Error updating user data after password reset."
    PASSWORD_RESET_FAILED = "First-time password change failed"
    REFRESH_TOKEN_INVALID = (
        "Invalid or expired refresh token. User has been logged out. Please log in again."
    )
    REFRESH_TOKEN_FAILED = "Token refresh failed. Please log in again."
    FORGOT_PASSWORD_FAILED = "Forgot password request failed"
    RESET_LINK_FAILED = "Failed to generate password reset link"
    EMAIL_SEND_FAILED = (
        "Failed to send password reset email. Please check email service configuration."
    )
    MISSING_AUTH_HEADER = "Missing or invalid authorization header"
    INVALID_TOKEN_NO_ID = "Invalid token: missing user ID"
    ACCOUNT_INACTIVE = "User account is not active or not approved"
    AUTHENTICATION_FAILED = "Authentication failed"
    LOGOUT_FAILED = "An unexpected error occurred during logout."


class SupabaseErrors:
    # Configuration / validation
    MISSING_CONFIG = "Missing required Supabase configuration: {}"
    INVALID_EMAIL = "Invalid email format"
    INVALID_PASSWORD = "Invalid password format"
    INVALID_USER_ID = "Invalid user ID format"
    INVALID_TOKEN = "Invalid token format"
    INVALID_HASHED_TOKEN = "Invalid hashed token format"
    # Operation failures
    SIGN_IN_FAILED = "Sign in failed: {}"
    RESET_LINK_FAILED = "Failed to generate password reset link: {}"
    HASH_TOKEN_GENERATION_FAILED = "Failed to generate hash token"
    USER_CREATION_FAILED = "User creation failed: {}"
    SIGN_OUT_FAILED = "Sign out failed: {}"
    TOKEN_VERIFICATION_FAILED = "Token verification failed: {}"
    TOKEN_REFRESH_FAILED = "Token refresh failed: {}"
    PASSWORD_RESET_FAILED = "Password reset failed: {}"
    TOKEN_VERIFICATION_FAILED_NO_ID = "Failed to get user ID from verification"
    TOKEN_VERIFICATION_FAILED_GENERIC = "Token verification failed"
    METADATA_UPDATE_FAILED = "Metadata update failed: {}"
    GET_USER_FROM_TOKEN_FAILED = "Failed to get user from token: {}"

    # Admin/auth-specific messages used by AdminAuth service
    MISSING_AUTHORIZATION_HEADER = "Missing or invalid Authorization header"
    MISSING_USER_ID_IN_TOKEN = "Missing user ID in token"
    ADMIN_NOT_FOUND = "Admin not found"
    ADMIN_ACCOUNT_NOT_ACTIVE = "Admin account is not active"
    AUTHENTICATION_FAILED = "Authentication failed"
    ADMIN_EMAIL_ALREADY_EXISTS = "Admin with this email already exists"
    FAILED_TO_CREATE_SUPABASE_USER = "Failed to create Supabase user"


class KnowledgeBaseErrors:
    UNEXPECTED_ERROR = GeneralErrors.UNEXPECTED_ERROR


class IntegrationErrors:
    """Error messages for integration operations."""

    INTEGRATION_NOT_FOUND = "Integration not found"
    INTEGRATION_NOT_AVAILABLE = "Integration is not available for connection yet"
    INTEGRATION_PROVIDER_NOT_CONFIGURED = "Integration provider is not configured"
    OAUTH_INITIATION_FAILED = "Failed to initiate OAuth flow"
    OAUTH_CALLBACK_FAILED = "OAuth callback processing failed"
    INTEGRATION_DISCONNECTION_FAILED = "Failed to disconnect integration"
    INTEGRATION_LIST_FAILED = "Failed to retrieve integrations"
    INTEGRATION_DETAILS_FAILED = "Failed to retrieve integration details"
    INTEGRATION_STATUS_CHECK_FAILED = "Failed to check integration status"
    INTEGRATION_UPDATE_FAILED = "Failed to update integration"
    AVAILABLE_INTEGRATIONS_FAILED = "Failed to retrieve available integrations"
    INVALID_CONNECTION_ID = "Invalid connection ID format"
    TOKEN_REFRESH_FAILED = "Failed to refresh integration token"
    SYNC_TRIGGER_FAILED = "Failed to trigger data sync"
