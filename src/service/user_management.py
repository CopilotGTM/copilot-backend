import secrets
from typing import Dict, Any, List, Optional
from uuid import UUID
from sqlmodel import Session, select
from sqlalchemy import or_, and_, func

from src.domain.models.master_db_models import User, Organisation, UserAppRole, SupabaseUserStatus, AdminUserStatus, UserActionHistory, UserEventType, OrganisationStatus
from src.domain.models.admin_user_models import CreateUserRequest, UpdateUserRequest
from src.infra.utils.logger import get_logger
from src.infra.utils.cursor import decode_cursor, encode_cursor
from src.infra.auth.supabase_auth import supabase_auth

logger = get_logger("user_management_service")

class UserManagementService:
    """Service for managing users within organizations."""

    def create_user(
        self,
        session: Session,
        admin_id: UUID,
        user_data: CreateUserRequest,
    ) -> User:
        """
        Creates a new user in the database.
        """
        logger.info(f"Creating user with email {user_data.email} by admin {admin_id}")

        # Check if organisation exists
        organisation = session.get(Organisation, user_data.organisation_id)
        if not organisation:
            raise ValueError(f"Organisation with ID {user_data.organisation_id} not found.")

        if organisation.status != OrganisationStatus.APPROVED:
            raise ValueError(f"Organisation with ID {user_data.organisation_id} is not approved.")

        # Check if user with email already exists
        existing_user = session.exec(select(User).where(User.email == user_data.email)).first()
        if existing_user:
            raise ValueError(f"User with email {user_data.email} already exists.")

        new_user = User(
            first_name=user_data.first_name,
            last_name=user_data.last_name,
            email=user_data.email,
            organisation_id=user_data.organisation_id,
            user_role=user_data.user_role, # Use user_role from CreateUserRequest
            job_title=user_data.job_title,
            organisation_role=user_data.organisation_role,
            supabase_user_id=None, # Supabase user will be created on approval
            status=SupabaseUserStatus.NOTHING, # Initial status
            admin_status=AdminUserStatus.PENDING, # Initial admin approval status
            created_by_auth_user_id=str(admin_id)
        )
        
        session.add(new_user)
        session.commit()
        session.refresh(new_user)

        # Log the user creation event
        history_entry = UserActionHistory(
            user_id=new_user.user_id,
            action=UserEventType.CREATED,
            actor_admin_id=admin_id,
            details={"message": f"User {new_user.email} created by admin {admin_id}."},
        )
        session.add(history_entry)
        session.commit()
        session.refresh(history_entry)
        
        logger.info(f"Successfully created user {new_user.user_id} with email {new_user.email}")
        return new_user

    def get_user(self, session: Session, user_id: UUID) -> Optional[User]:
        """
        Retrieves a single user by their ID.
        """
        logger.info(f"Fetching user with ID: {user_id}")
        user = session.get(User, user_id)
        return user

    def list_users(
        self,
        session: Session,
        limit: int,
        cursor: Optional[str] = None,
        organisation_id: Optional[UUID] = None,
    ) -> List[User]:
        """
        Retrieves a list of users with pagination.
        """
        logger.info("Fetching list of users")

        filters = []
        if organisation_id:
            filters.append(User.organisation_id == organisation_id)

        if cursor:
            ts, user_id = decode_cursor(cursor)
            filters.append(
                or_(
                    User.created_at < ts,
                    and_(
                        User.created_at == ts,
                        User.user_id < user_id,
                    ),
                )
            )

        stmt = (
            select(User)
            .where(*filters)
            .order_by(User.created_at.desc(), User.user_id.desc())
            .limit(limit + 1)
        )

        users = session.exec(stmt).all()
        return users

    def list_user_action_history_for_organisation(
        self,
        session: Session,
        organisation_id: UUID,
        limit: int,
        cursor: Optional[str] = None,
    ) -> List[UserActionHistory]:
        """
        Retrieves the action history for all users in an organisation.
        """
        logger.info(f"Fetching user action history for organisation ID: {organisation_id}")

        query = (
            select(UserActionHistory)
            .join(User)
            .where(User.organisation_id == organisation_id)
        )

        if cursor:
            ts, history_id = decode_cursor(cursor)
            query = query.where(
                or_(
                    UserActionHistory.created_at < ts,
                    and_(
                        UserActionHistory.created_at == ts,
                        UserActionHistory.id < history_id,
                    ),
                )
            )

        query = query.order_by(UserActionHistory.created_at.desc(), UserActionHistory.id.desc()).limit(limit + 1)

        history = session.exec(query).all()
        return history

    def update_user_admin_status(
        self,
        session: Session,
        user_id: UUID,
        admin_id: UUID,
        new_admin_status: AdminUserStatus,
        note: Optional[str] = None,
    ) -> User:
        """
        Updates the admin status of a user and logs the action.
        Sends a password reset email if the status changes to APPROVED.
        """
        logger.info(f"Updating admin status of user {user_id} to '{new_admin_status}' by admin {admin_id}")

        user = session.get(User, user_id)
        if not user:
            raise ValueError(f"User with ID {user_id} not found.")

        if user.admin_status == new_admin_status:
            logger.warning(f"User {user_id} is already in admin status '{new_admin_status}'.")
            return user

        # State transition logic for admin_status
        if new_admin_status == AdminUserStatus.APPROVED:
            if user.admin_status != AdminUserStatus.PENDING:
                raise ValueError(f"User must be in PENDING admin status to be approved, but is in {user.admin_status} state.")
            
            if not user.supabase_user_id:
                logger.info(f"User {user.user_id} does not have a Supabase account. Creating one.")
                random_password = secrets.token_urlsafe(16)
                user_metadata = {
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "organisation_id": str(user.organisation_id),
                    "user_role": user.user_role,
                    "job_title": user.job_title,
                }
                supabase_response = supabase_auth.create_user_with_service_role(
                    email=user.email,
                    password=random_password,
                    user_metadata=user_metadata,
                )
                if not supabase_response or not supabase_response.get("id"):
                    logger.error(f"Failed to create user in Supabase for email: {user.email}")
                    raise Exception("Failed to create user in authentication system.")
                
                user.supabase_user_id = supabase_response["id"]
                user.status = SupabaseUserStatus.PENDING_VERIFICATION
                logger.info(f"Supabase user created with ID: {user.supabase_user_id}")

            # Generate password reset link and send email
            reset_link = supabase_auth.generate_password_reset_link(
                user_id=user.supabase_user_id,
                email=user.email,
                # TODO: Configure a proper redirect_to URL for the frontend
                redirect_to="http://localhost:3000/reset-password"
            )
            if reset_link:
                # In a real application, you would send this link via an email service
                logger.info(f"Password reset link for {user.email}: {reset_link}")
                # Example: email_service.send_password_reset_email(user.email, reset_link)
            else:
                logger.error(f"Failed to generate password reset link for user {user.email}")
                # Decide how to handle this failure: raise error, log and continue, etc.

            user.approved_by = admin_id
            user.approved_at = func.now()

            event_type = UserEventType.STATUS_CHANGED
            details = {"message": f"Admin status changed to {new_admin_status} by admin {admin_id}.", "previous_status": user.admin_status.value}

        elif new_admin_status == AdminUserStatus.REJECTED:
            if user.admin_status != AdminUserStatus.PENDING:
                raise ValueError(f"User must be in PENDING admin status to be rejected, but is in {user.admin_status} state.")
            event_type = UserEventType.STATUS_CHANGED
            details = {"message": f"Admin status changed to {new_admin_status} by admin {admin_id}.", "previous_status": user.admin_status.value}

        elif new_admin_status == AdminUserStatus.SUSPENDED:
            if user.admin_status != AdminUserStatus.APPROVED:
                raise ValueError(f"User must be in APPROVED admin status to be suspended, but is in {user.admin_status} state.")
            event_type = UserEventType.STATUS_CHANGED
            details = {"message": f"Admin status changed to {new_admin_status} by admin {admin_id}.", "previous_status": user.admin_status.value}
        
        elif new_admin_status == AdminUserStatus.PENDING:
            # Allow transition back to PENDING from REJECTED or SUSPENDED if needed, or restrict
            if user.admin_status not in [AdminUserStatus.REJECTED, AdminUserStatus.SUSPENDED]:
                raise ValueError(f"Cannot change admin status to PENDING from {user.admin_status} state.")
            event_type = UserEventType.STATUS_CHANGED
            details = {"message": f"Admin status changed to {new_admin_status} by admin {admin_id}.", "previous_status": user.admin_status.value}

        else:
            raise ValueError(f"Unsupported admin status transition to '{new_admin_status}'.")

        user.admin_status = new_admin_status
        
        history_entry = UserActionHistory(
            user_id=user_id,
            action=event_type,
            actor_admin_id=admin_id,
            note=note,
            details=details,
        )
        session.add(history_entry)
        session.commit()
        session.refresh(user)
        logger.info(f"Successfully updated user {user_id} admin status to '{new_admin_status}'")
        return user

    def update_user(
        self,
        session: Session,
        user_id: UUID,
        admin_id: UUID,
        user_data: UpdateUserRequest,
    ) -> User:
        """
        Updates a user's details.
        """
        logger.info(f"Updating user {user_id} by admin {admin_id}")

        user = session.get(User, user_id)
        if not user:
            raise ValueError(f"User with ID {user_id} not found.")

        updated_fields = []
        for field, value in user_data.dict(exclude_unset=True).items():
            if getattr(user, field) != value:
                setattr(user, field, value)
                updated_fields.append(field)

        if updated_fields:
            history_entry = UserActionHistory(
                user_id=user_id,
                action=UserEventType.UPDATED,
                actor_admin_id=admin_id,
                details={"updated_fields": updated_fields},
            )
            session.add(history_entry)
            session.commit()
            session.refresh(user)
            logger.info(f"Successfully updated user {user_id}. Fields changed: {', '.join(updated_fields)}")
        else:
            logger.info(f"No changes detected for user {user_id}.")

        return user

user_management_service = UserManagementService()
