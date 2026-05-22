"""Runtime policy resolution for agent action approvals.

Agent tools call this service before performing side effects. It combines the
product defaults, the user's saved preferences, and organisation-level CRM write
policy so the tool layer has one decision: run directly, ask for approval, or
deny the action.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col
from src.domain.models.master_tables.organisation_models import Organisation
from src.domain.models.master_tables.user_models import User
from src.service.agentic_approvals.action_approval_defaults import get_action_approval_default
from src.service.agentic_approvals.action_approval_service import AgentActionApprovalService
from src.service.orgnisation.organisation_preferences import has_crm_write_scope


@dataclass(frozen=True)
class ActionApprovalDecision:
    """Resolved decision for a single agent action in one user/org context."""

    tool_name: str
    allowed: bool
    approval_required: bool
    locked: bool = False
    unknown_tool: bool = False
    org_policy_denied: bool = False
    code: str | None = None
    reason: str | None = None

    @property
    def allowed_direct(self) -> bool:
        """True when the tool may execute without an approval round trip."""

        return self.allowed and not self.approval_required


def resolve_action_approval_decision(
    *,
    tool_name: str,
    user_preferences: dict[str, Any] | None,
    organisation_preferences: dict[str, Any] | None,
) -> ActionApprovalDecision:
    """Merge defaults, user preferences, and organisation policy for one tool."""

    default = get_action_approval_default(tool_name)
    if default is None:
        return ActionApprovalDecision(
            tool_name=tool_name,
            allowed=False,
            approval_required=True,
            unknown_tool=True,
            code="unknown_action_tool",
            reason="Unknown agent action tools are blocked by default.",
        )

    if default.crm_write_scope and not has_crm_write_scope(
        organisation_preferences,
        default.crm_write_scope,
    ):
        return ActionApprovalDecision(
            tool_name=tool_name,
            allowed=False,
            approval_required=True,
            org_policy_denied=True,
            code="crm_write_scope_disabled",
            reason=(f"Organisation policy disables CRM write scope '{default.crm_write_scope}'."),
        )

    effective = {
        item["tool_name"]: item
        for item in AgentActionApprovalService.build_effective_preferences(user_preferences)
    }
    tool_preferences = effective[tool_name]
    return ActionApprovalDecision(
        tool_name=tool_name,
        allowed=True,
        approval_required=bool(tool_preferences["approval_required"]),
        locked=not default.editable,
    )


class AgentActionApprovalPolicyService:
    """Load master-table policy context and resolve action approval decisions."""

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

    async def resolve(self, tool_name: str) -> ActionApprovalDecision:
        """Resolve whether the current user may run the tool directly."""

        user = await self._get_user()
        if user is None:
            return ActionApprovalDecision(
                tool_name=tool_name,
                allowed=False,
                approval_required=True,
                code="user_not_found",
                reason="User preference policy could not be loaded.",
            )
        organisation = await self._get_organisation()
        if organisation is None:
            return ActionApprovalDecision(
                tool_name=tool_name,
                allowed=False,
                approval_required=True,
                code="organisation_not_found",
                reason="Organisation policy could not be loaded.",
            )
        return resolve_action_approval_decision(
            tool_name=tool_name,
            user_preferences=user.preferences,
            organisation_preferences=organisation.preferences,
        )

    async def _get_user(self) -> User | None:
        result = await self.master_session.execute(
            select(User).where(
                # User preferences are tenant-owned. Matching both IDs prevents
                # a stale or spoofed user ID from loading another org's policy.
                col(User.user_id) == self.user_id,
                col(User.organisation_id) == self.organisation_id,
            )
        )
        return result.scalar_one_or_none()

    async def _get_organisation(self) -> Organisation | None:
        result = await self.master_session.execute(
            select(Organisation).where(col(Organisation.organisation_id) == self.organisation_id)
        )
        return result.scalar_one_or_none()
