# Authentication API Documentation

This document provides comprehensive documentation for the authentication system using Supabase integration.

## Table of Contents

- [Overview](#overview)
- [Authentication Flow](#authentication-flow)
- [API Endpoints](#api-endpoints)
- [Models](#models)
- [Error Handling](#error-handling)
- [Examples](#examples)

## Overview

The authentication system uses Supabase as the backend authentication service with a custom FastAPI wrapper. The system supports:

- **Admin Authentication**: Super admin login/logout and password management
- **User Authentication**: Regular user authentication via cookies
- **Password Management**: Reset, change, and forgot password flows
- **Token Management**: Access and refresh token handling

## Authentication Flow

### Admin Authentication Flow

1. **Login**: Admin provides email/password → Supabase validates → Returns user data and tokens
2. **PENDING_SETUP**: New admins must change password on first login
3. **Password Reset**: Uses Supabase recovery tokens for secure password changes
4. **Logout**: Invalidates current session or all sessions globally

### User Authentication Flow

1. **Cookie-based**: Uses `auth-storage` cookie for session management
2. **Token Verification**: Validates Supabase tokens and maps to local users
3. **WebSocket Support**: Token-based authentication for real-time connections

## API Endpoints

### Admin Authentication Endpoints

#### POST `/admin/login`

**Description**: Super admin login endpoint

**Request Body**:
```json
{
  "email": "admin@example.com",
  "password": "password123"
}
```

**Response for ACTIVE users**:
```json
{
  "success": true,
  "message": "Login successful",
  "data": {
    "email": "admin@example.com",
    "first_name": "John",
    "last_name": "Doe",
    "role": "super_admin",
    "state": "ACTIVE",
    "created_at": "2024-01-01T00:00:00Z",
    "email_verified": true,
    "last_login_at": "2024-01-01T12:00:00Z",
    "tokens": {
      "access_token": "eyJ...",
      "refresh_token": "eyJ..."
    }
  }
}
```

**Response for PENDING_SETUP users**:
```json
{
  "success": true,
  "message": "Login successful",
  "data": {
    "state": "PENDING_SETUP",
    "password_reset_link": "http://localhost:3000/reset-password?token_hash=abc123..."
  }
}
```

#### POST `/admin/reset-password`

**Description**: Password reset endpoint for both force change and forgot password

**Request Body**:
```json
{
  "action": "force_change",
  "email": "admin@example.com",
  "new_password": "newpassword123",
  "confirm_password": "newpassword123",
  "token_hash": "abc123..."
}
```

**Actions**:
- `force_change`: For PENDING_SETUP users, updates metadata and local DB
- `forgot`: For regular password reset, only changes password

**Response**:
```json
{
  "success": true,
  "message": "Password changed successfully. You can now log in with your new password."
}
```

#### POST `/admin/refresh-token`

**Description**: Refresh access token using refresh token

**Request Body**:
```json
{
  "refresh_token": "eyJ..."
}
```

**Response**:
```json
{
  "success": true,
  "message": "Token refreshed successfully",
  "data": {
    "access_token": "eyJ...",
    "refresh_token": "eyJ..."
  }
}
```

**Note**: Automatically logs out user if refresh token is invalid.

#### POST `/admin/logout`

**Description**: Logout current session

**Headers**: `Authorization: Bearer <access_token>`

**Response**:
```json
{
  "success": true,
  "message": "Successfully logged out."
}
```

#### POST `/admin/global-logout`

**Description**: Logout user from all sessions globally

**Request Body**:
```json
{
  "email": "user@example.com"
}
```

**Headers**: `Authorization: Bearer <access_token>`

**Response**:
```json
{
  "success": true,
  "message": "Successfully logged out user user@example.com from all sessions."
}
```

#### POST `/admin/forgot-password`

**Description**: Send password reset email

**Request Body**:
```json
{
  "email": "admin@example.com"
}
```

**Response**:
```json
{
  "success": true,
  "message": "If an account with this email exists, a password reset link has been sent."
}
```

#### POST `/admin/change-password`

**Description**: Change password for logged-in admin

**Request Body**:
```json
{
  "current_password": "oldpassword",
  "new_password": "newpassword123"
}
```

**Headers**: `Authorization: Bearer <access_token>`

**Response**:
```json
{
  "success": true,
  "message": "Password changed successfully."
}
```

### User Authentication Endpoints

#### WebSocket Authentication

**Description**: Token-based authentication for WebSocket connections

**Token Format**: JWT token from Supabase

**Response**:
```json
{
  "supabase_user_id": "user-uuid",
  "user_id": "local-user-id",
  "organisation_id": "org-uuid"
}
```

## Models

### Admin Models

#### AdminLoginRequest
```python
class AdminLoginRequest(SQLModel):
    email: EmailStr
    password: str
```

#### AdminLoginResponse
```python
class AdminLoginResponse(SQLModel):
    email: Optional[EmailStr] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    role: Optional[AdminRole] = None
    state: str
    last_login_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    email_verified: Optional[bool] = None
    tokens: Optional[Tokens] = None
    password_reset_link: Optional[str] = None
```

#### AdminResetPasswordRequest
```python
class AdminResetPasswordRequest(BaseModel):
    action: PasswordResetAction
    email: EmailStr
    new_password: str
    confirm_password: str
    token_hash: str
```

#### PasswordResetAction Enum
```python
class PasswordResetAction(str, Enum):
    FORGOT = "forgot"
    FORCE_CHANGE = "force_change"
```

#### AdminLoginState Enum
```python
class AdminLoginState(str, Enum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    PENDING_SETUP = "PENDING_SETUP"
```

### User Models

#### User Authentication
```python
class Authentication:
    cookie_name: str = "auth-storage"
    
    async def verify_session(self, master_session: Session, auth_storage: Optional[str] = Cookie(None)) -> Dict[str, Any]
    def decode_for_ws(self, token: str) -> Dict[str, Any]
```

## Error Handling

### HTTP Status Codes

- **200**: Success
- **400**: Bad Request (invalid input)
- **401**: Unauthorized (invalid credentials/token)
- **403**: Forbidden (missing authentication)
- **404**: Not Found
- **500**: Internal Server Error

### Error Response Format

```json
{
  "success": false,
  "message": "Error description",
  "data": null
}
```

### Common Error Messages

- `"User not found"`: Email doesn't exist in database
- `"Invalid credentials"`: Wrong email/password combination
- `"Invalid or expired recovery token"`: Password reset token is invalid
- `"Authentication token missing"`: No authorization header or cookie
- `"Admin account is not active"`: Admin status is not ACTIVE

## Examples

### Complete Login Flow

1. **Login with temporary password**:
```bash
curl -X POST "http://localhost:8000/admin/login" \
  -H "Content-Type: application/json" \
  -d '{"email": "admin@example.com", "password": "temp123"}'
```

2. **Reset password** (for PENDING_SETUP):
```bash
curl -X POST "http://localhost:8000/admin/reset-password" \
  -H "Content-Type: application/json" \
  -d '{
    "action": "force_change",
    "email": "admin@example.com",
    "new_password": "newpassword123",
    "confirm_password": "newpassword123",
    "token_hash": "abc123..."
  }'
```

3. **Login with new password**:
```bash
curl -X POST "http://localhost:8000/admin/login" \
  -H "Content-Type: application/json" \
  -d '{"email": "admin@example.com", "password": "newpassword123"}'
```

### Token Management

1. **Refresh token**:
```bash
curl -X POST "http://localhost:8000/admin/refresh-token" \
  -H "Content-Type: application/json" \
  -d '{"refresh_token": "eyJ..."}'
```

2. **Logout**:
```bash
curl -X POST "http://localhost:8000/admin/logout" \
  -H "Authorization: Bearer eyJ..."
```

### Password Management

1. **Forgot password**:
```bash
curl -X POST "http://localhost:8000/admin/forgot-password" \
  -H "Content-Type: application/json" \
  -d '{"email": "admin@example.com"}'
```

2. **Change password**:
```bash
curl -X POST "http://localhost:8000/admin/change-password" \
  -H "Authorization: Bearer eyJ..." \
  -H "Content-Type: application/json" \
  -d '{
    "current_password": "oldpassword",
    "new_password": "newpassword123"
  }'
```

## Security Features

### Password Requirements
- Minimum 6 characters
- Validated on both client and server

### Token Security
- JWT tokens with proper expiration
- Automatic logout on invalid tokens
- Global logout capability

### Session Management
- Cookie-based sessions for HTTP
- Token-based for WebSocket
- Proper session invalidation

### Error Handling
- No user enumeration (generic error messages)
- Comprehensive logging
- Graceful error recovery

## Configuration

### Environment Variables

```bash
# Supabase Configuration
SUPABASE_URL=your_supabase_url
SUPABASE_ANON_KEY=your_anon_key
SUPABASE_SERVICE_ROLE_KEY=your_service_role_key
SUPABASE_JWT_SECRET=your_jwt_secret

# Application Configuration
APP_URL=http://localhost:3000
ENABLE_SUPABASE_AUTH=true
```

### Database Models

The authentication system works with these database models:

- `SuperAdmin`: Admin users with roles and status
- `User`: Regular users with organization mapping
- `Organisation`: Organization management

## Troubleshooting

### Common Issues

1. **"Invalid credentials"**: Check email/password combination
2. **"Token verification failed"**: Token may be expired or invalid
3. **"Admin not found"**: User exists in Supabase but not in local DB
4. **"Password reset link invalid"**: Link may be expired or already used

### Debug Steps

1. Check Supabase logs for authentication errors
2. Verify environment variables are set correctly
3. Ensure database models are properly migrated
4. Check token expiration times

## Migration Notes

This system has been migrated from a custom HTTP-based Supabase integration to the official Supabase Python library for better maintainability and reliability.

### Key Changes
- Uses `supabase_client` instead of `supabase_auth`
- Official Supabase Python library integration
- Improved error handling and type safety
- Cleaner code structure with proper separation of concerns 