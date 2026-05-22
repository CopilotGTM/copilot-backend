"""Application service for user-owned agent action approval preferences.

The API layer delegates to this module so reads and writes consistently enforce
the authenticated user's organisation boundary before touching the master
`users.preferences` JSON blob. The service also owns the merge rules for the
`agent_action_approvals` branch so API reads, API writes, and runtime policy
checks interpret stored preferences the same way.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.api.schemas.agent_action_approval_models import (
    AgentActionApprovalPreferenceItem,
    AgentActionApprovalPreferencesResponse,
    UpdateAgentActionApprovalPreferencesRequest,
)
from src.domain.models.master_tables.user_models import User
from src.service.agentic_approvals.action_approval_defaults import ACTION_APPROVAL_DEFAULTS

AGENT_ACTION_APPROVALS_KEY = "agent_action_approvals"
SCHEMA_VERSION = 1


class AgentActionApprovalService:
    """Read, merge, and update the current user's agent action approval settings."""

    def __init__(
        self,
        *,
        master_session: AsyncSession,
        organisation_id: UUID,
        user_id: UUID,
    ) -> None:
        self.master_session = master_session
        self.organisation_id = organisation_id
        self.user_id = user_id

    @staticmethod
    def build_effective_preferences(
        preferences: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """Return UI-ready rows after applying saved values over product defaults.

        Locked defaults always win so a tampered or stale preferences blob cannot
        disable mandatory approval for high-risk actions.
        """

        root = preferences if isinstance(preferences, dict) else {}
        branch = root.get(AGENT_ACTION_APPROVALS_KEY)
        saved = branch.get("tools") if isinstance(branch, dict) else {}
        if not isinstance(saved, dict):
            saved = {}

        rows: list[dict[str, Any]] = []

        for tool_name, default in ACTION_APPROVAL_DEFAULTS.items():
            raw_saved = saved.get(tool_name)
            saved_value = None
            # Accept the current object shape and the older boolean shorthand. This
            # keeps existing user rows readable while the branch is rewritten below.
            if isinstance(raw_saved, dict) and isinstance(raw_saved.get("approval_required"), bool):
                saved_value = raw_saved["approval_required"]
            elif isinstance(raw_saved, bool):
                saved_value = raw_saved

            approval_required = (
                default.approval_required
                if saved_value is None or not default.editable
                else saved_value
            )
            rows.append(
                {
                    "tool_name": tool_name,
                    "label": default.label,
                    "category": default.category,
                    "approval_required": approval_required,
                    "default_approval_required": default.approval_required,
                    "editable": default.editable,
                    "locked_reason": default.locked_reason,
                }
            )

        return rows

    @staticmethod
    def merge_preferences(
        preferences: dict[str, Any] | None,
        updates: dict[str, bool],
    ) -> dict[str, Any]:
        """Merge editable approval toggles back into the user's preferences JSON.

        The function rewrites the owned branch to include every known tool. That
        keeps future reads deterministic and makes removed or locked tools fall
        back to server defaults instead of trusting client state.
        """

        root = dict(preferences or {})
        branch = root.get(AGENT_ACTION_APPROVALS_KEY)
        existing_tools = branch.get("tools") if isinstance(branch, dict) else {}
        if not isinstance(existing_tools, dict):
            existing_tools = {}

        next_tools: dict[str, dict[str, bool]] = {}

        for tool_name, default in ACTION_APPROVAL_DEFAULTS.items():
            raw_existing = existing_tools.get(tool_name)
            current_value = None
            # The saved branch is user-controlled JSON, so only explicit booleans
            # participate in the merge. Anything else falls back to code defaults.
            if isinstance(raw_existing, dict) and isinstance(
                raw_existing.get("approval_required"),
                bool,
            ):
                current_value = raw_existing["approval_required"]
            elif isinstance(raw_existing, bool):
                current_value = raw_existing

            approval_required = (
                default.approval_required if current_value is None else current_value
            )

            if tool_name in updates and default.editable:
                approval_required = updates[tool_name]
            if not default.editable:
                approval_required = default.approval_required

            next_tools[tool_name] = {"approval_required": approval_required}

        root[AGENT_ACTION_APPROVALS_KEY] = {
            "schema_version": SCHEMA_VERSION,
            "tools": next_tools,
        }
        return root

    async def get_preferences(self) -> AgentActionApprovalPreferencesResponse:
        """Load effective settings for the current user."""

        user = await self._get_current_user_row()
        return AgentActionApprovalPreferencesResponse(
            tools=[
                AgentActionApprovalPreferenceItem.model_validate(row)
                for row in self.build_effective_preferences(user.preferences)
            ]
        )

    async def update_preferences(
        self,
        request: UpdateAgentActionApprovalPreferencesRequest,
    ) -> AgentActionApprovalPreferencesResponse:
        """Persist editable settings and return the effective post-update view."""

        user = await self._get_current_user_row()
        user.preferences = self.merge_preferences(user.preferences, request.tools)
        await self.master_session.commit()
        await self.master_session.refresh(user)
        return AgentActionApprovalPreferencesResponse(
            tools=[
                AgentActionApprovalPreferenceItem.model_validate(row)
                for row in self.build_effective_preferences(user.preferences)
            ]
        )

    async def _get_current_user_row(self) -> User:
        result = await self.master_session.execute(
            select(User).where(
                # Settings are scoped to the authenticated org/user pair; a user
                # ID alone is not trusted across the shared public schema.
                col(User.user_id) == self.user_id,
                col(User.organisation_id) == self.organisation_id,
            )
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        return user
