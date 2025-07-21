# from typing import Optional
# from fastapi import APIRouter, Depends, HTTPException, status, Request
# from sqlmodel import Session
# from uuid import UUID

# from src.domain.models.admin_models import (
#     AdminLoginRequest,
#     AdminResponse,
#     AdminCreateRequest,
#     AdminUpdateRequest,
#     AdminStatus,
#     SuperAdmin
# )
# from src.infra.utils.database import get_master_session
# from src.infra.utils.admin_auth import admin_auth
# from src.infra.utils.email_service import email_service
# from src.infra.utils.supabase_auth import supabase_auth
# from src.infra.utils.logger import get_logger
# from sqlmodel import select
# from src.api.utils.http_response import Response

# logger = get_logger()
# router = APIRouter()

# @router.post("/login")
# async def admin_login(
#     login_request: AdminLoginRequest,
#     master_session: Session = Depends(get_master_session),
# ):
#     """
#     Super Admin login endpoint.
#     Accepts email and password, verifies credentials, and returns super admin details,
#     Supabase session token (access_token), and requires_password_change flag.
#     The access_token must be used for authenticated requests such as first-time password change.
#     """
#     try:
#         logger.debug(f"Received super admin login request: email={login_request.email}")

#         # Fetch super admin by email
#         statement = select(SuperAdmin).where(SuperAdmin.email == login_request.email)
#         logger.debug(f"Executing DB select for SuperAdmin with email: {login_request.email}")
#         result = master_session.exec(statement)
#         super_admin = result.first()
#         logger.debug(f"Super admin DB result: {super_admin}")

#         if not super_admin:
#             logger.warning(f"Super admin not found for email: {login_request.email}")
#             return Response.bad_request_error_response("Super admin not found")

#         # Verify password using Supabase Auth
#         from src.infra.utils.supabase_auth import supabase_auth
#         logger.debug("Calling supabase_auth.verify_email_password()")
#         supabase_response = supabase_auth.verify_email_password(
#             email=login_request.email, password=login_request.password
#         )
#         logger.debug(f"Supabase response: {supabase_response}")
#         access_token = supabase_response.get("access_token")
#         refresh_token = supabase_response.get("refresh_token")
#         if not access_token:
#             logger.warning(f"Invalid credentials for super admin: {login_request.email}")
#             return Response.bad_request_error_response("Invalid credentials")

#         # Update last login timestamp
#         from datetime import datetime
#         logger.debug(f"Updating last_login_at for super_admin: {super_admin.email}")
#         super_admin.last_login_at = datetime.utcnow()
#         master_session.add(super_admin)
#         master_session.commit()
#         master_session.refresh(super_admin)
#         logger.debug(f"Updated super_admin after commit: {super_admin}")

#         logger.info(f"Super admin login successful: {super_admin.email}")

#         # Check if password change is required (pending_setup)
#         requires_password_change = (
#             str(super_admin.status) == "pending_setup"
#         )
#         logger.debug(f"Requires password change: {requires_password_change}")

#         # Return admin details + requires_password_change flag + session tokens
#         response_data = {
#             "admin_id": super_admin.admin_id,
#             "supabase_user_id": super_admin.supabase_user_id,
#             "email": super_admin.email,
#             "first_name": super_admin.first_name,
#             "last_name": super_admin.last_name,
#             "role": super_admin.role,
#             "status": super_admin.status,
#             "last_login_at": super_admin.last_login_at,
#             "created_at": super_admin.created_at,
#             "email_verified": super_admin.email_verified,
#             "requires_password_change": requires_password_change,
#             "access_token": access_token,
#             "refresh_token": refresh_token,
#         }
#         logger.info(f"Returning response data for super admin: {super_admin.email}")
#         logger.debug(f"Response data: {response_data}")
#         return response_data

#     except Exception as e:
#         logger.error(f"Error during super admin login: {str(e)}", exc_info=True)
#         from src.api.utils.http_response import Response
#         return Response.internal_server_error_response("Super admin login failed")

# async def get_current_admin_from_supabase(
#     request: Request,
#     session: Session = Depends(get_master_session)
# ):
#     """
#     Get current admin from Supabase authentication
#     """
#     try:
#         # Get the authorization header
#         auth_header = request.headers.get("Authorization")
#         if not auth_header or not auth_header.startswith("Bearer "):
#             raise HTTPException(
#                 status_code=status.HTTP_401_UNAUTHORIZED,
#                 detail="Missing or invalid authorization header"
#             )

#         # Extract token
#         token = auth_header.split(" ")[1]

#         # Verify token with Supabase
#         payload = supabase_auth.verify_token(token)
#         supabase_user_id = payload.get("sub")

#         if not supabase_user_id:
#             raise HTTPException(
#                 status_code=status.HTTP_401_UNAUTHORIZED,
#                 detail="Invalid token"
#             )

#         # Get admin from database
#         admin = admin_auth.get_admin_by_supabase_id(supabase_user_id, session)
#         if not admin:
#             raise HTTPException(
#                 status_code=status.HTTP_401_UNAUTHORIZED,
#                 detail="Admin not found"
#             )

#         # Update last login time
#         admin_auth.update_admin_login_time(admin.admin_id, session)

#         return admin
#     except HTTPException:
#         raise
#     except Exception as e:
#         logger.error(f"Error getting current admin: {str(e)}")
#         raise HTTPException(
#             status_code=status.HTTP_401_UNAUTHORIZED,
#             detail="Authentication failed"
#         )


# @router.get("/me", response_model=AdminResponse)
# async def get_current_admin_info(
#     admin: SuperAdmin = Depends(get_current_admin_from_supabase)
# ):
#     """
#     Get current admin information
#     """
#     return AdminResponse(
#         admin_id=admin.admin_id,
#         supabase_user_id=admin.supabase_user_id,
#         email=admin.email,
#         first_name=admin.first_name,
#         last_name=admin.last_name,
#         role=admin.role,
#         status=admin.status,
#         last_login_at=admin.last_login_at,
#         created_at=admin.created_at,
#         email_verified=admin.email_verified
#     )


# @router.get("/requires-password-change")
# async def check_requires_password_change(
#     admin: SuperAdmin = Depends(get_current_admin_from_supabase)
# ):
#     """
#     Check if admin requires password change on first login
#     """
#     requires_change = admin_auth.check_requires_password_change(admin.supabase_user_id)
#     return {
#         "requires_password_change": requires_change,
#         "admin_id": str(admin.admin_id)
#     }


# from pydantic import BaseModel

# class FirstTimePasswordChangeRequest(BaseModel):
#     new_password: str

# @router.post("/first-time-password-change")
# async def first_time_password_change(
#     request: Request,
#     body: FirstTimePasswordChangeRequest,
#     session: Session = Depends(get_master_session)
# ):
#     """
#     First time password change for super admin.
#     Requires Authorization: Bearer <access_token> header.
#     Only the authenticated user can change their own password.
#     """
#     try:
#         # Authenticate user via token (JWT in Authorization header)
#         admin = await get_current_admin_from_supabase(request, session)
#         auth_header = request.headers.get("Authorization")
#         if not auth_header or not auth_header.startswith("Bearer "):
#             raise HTTPException(
#                 status_code=status.HTTP_401_UNAUTHORIZED,
#                 detail="Missing or invalid authorization header"
#             )
#         token = auth_header.split(" ")[1]
#         # Call supabase_auth to update password
#         supabase_result = supabase_auth.update_password(token, body.new_password)
#         if not supabase_result.get("user"):
#             raise HTTPException(
#                 status_code=status.HTTP_400_BAD_REQUEST,
#                 detail="Failed to update password"
#             )
#         # Only allow if status is pending_setup
#         if admin.status == AdminStatus.PENDING_SETUP:
#             admin.status = AdminStatus.ACTIVE
#             session.add(admin)
#             session.commit()
#             session.refresh(admin)
#         return {"message": "Password updated successfully"}
#     except HTTPException:
#         raise
#     except Exception as e:
#         logger.error(f"First time password change error: {str(e)}")
#         raise HTTPException(
#             status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
#             detail="Failed to change password"
#         )


# @router.post("/create", response_model=AdminResponse)
# async def create_admin(
#     admin_data: AdminCreateRequest,
#     current_admin: SuperAdmin = Depends(get_current_admin_from_supabase),
#     session: Session = Depends(get_master_session)
# ):
#     """
#     Create a new admin (only super admins can create other admins)
#     """
#     try:
#         # Only super admins can create other admins
#         if current_admin.role != "super_admin":
#             raise HTTPException(
#                 status_code=status.HTTP_403_FORBIDDEN,
#                 detail="Only super admins can create other admins"
#             )

#         admin, temp_password = admin_auth.create_super_admin(
#             admin_data.email,
#             admin_data.first_name,
#             admin_data.last_name,
#             session
#         )

#         # Send email with temporary password
#         email_sent = email_service.send_super_admin_creation_email(
#             admin.email,
#             admin.first_name,
#             temp_password
#         )

#         if not email_sent:
#             logger.warning(f"Failed to send email to {admin.email}")

#         return AdminResponse(
#             admin_id=admin.admin_id,
#             supabase_user_id=admin.supabase_user_id,
#             email=admin.email,
#             first_name=admin.first_name,
#             last_name=admin.last_name,
#             role=admin.role,
#             status=admin.status,
#             last_login_at=admin.last_login_at,
#             created_at=admin.created_at,
#             email_verified=admin.email_verified
#         )

#     except HTTPException:
#         raise
#     except Exception as e:
#         logger.error(f"Create admin error: {str(e)}")
#         raise HTTPException(
#             status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
#             detail="Failed to create admin"
#         )


# @router.post("/forgot-password")
# async def forgot_password(
#     email: str,
#     session: Session = Depends(get_master_session)
# ):
#     """
#     Request password reset - This would typically be handled by Supabase
#     """
#     try:
#         admin = admin_auth.get_admin_by_email(email, session)
#         if not admin:
#             # Don't reveal if email exists or not
#             return {"message": "If the email exists, a password reset link has been sent"}

#         # Generate password reset URL (this would use Supabase's password reset)
#         reset_url = f"{admin_auth.app_url}/admin/reset-password"

#         # Send email
#         email_sent = email_service.send_password_reset_email(
#             admin.email,
#             admin.first_name,
#             reset_url
#         )

#         if email_sent:
#             return {"message": "Password reset email sent successfully"}
#         else:
#             raise HTTPException(
#                 status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
#                 detail="Failed to send password reset email"
#             )

#     except HTTPException:
#         raise
#     except Exception as e:
#         logger.error(f"Forgot password error: {str(e)}")
#         raise HTTPException(
#             status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
#             detail="Password reset request failed"
#         )


# @router.put("/status")
# async def update_admin_status(
#     admin_id: UUID,
#     status: AdminStatus,
#     current_admin: SuperAdmin = Depends(get_current_admin_from_supabase),
#     session: Session = Depends(get_master_session)
# ):
#     """
#     Update admin status (only super admins can update other admins)
#     """
#     try:
#         # Only super admins can update other admins
#         if current_admin.role != "super_admin":
#             raise HTTPException(
#                 status_code=status.HTTP_403_FORBIDDEN,
#                 detail="Only super admins can update admin status"
#             )

#         success = admin_auth.update_admin_status(admin_id, status, session)

#         if not success:
#             raise HTTPException(
#                 status_code=status.HTTP_404_NOT_FOUND,
#                 detail="Admin not found"
#             )

#         return {"message": "Admin status updated successfully"}

#     except HTTPException:
#         raise
#     except Exception as e:
#         logger.error(f"Update admin status error: {str(e)}")
#         raise HTTPException(
#             status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
#             detail="Failed to update admin status"
#         )
