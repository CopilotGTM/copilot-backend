from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field
from src.domain.models.master_tables import UserAuthenticationMethod

DomainJoinPolicy = Literal["open", "invite_only"]


class MembershipPolicyResponse(BaseModel):
    domain_join_policy: DomainJoinPolicy


class UpdateMembershipPolicyRequest(BaseModel):
    domain_join_policy: DomainJoinPolicy

    model_config = ConfigDict(extra="forbid")


class OrganisationMemberInvitationRequest(BaseModel):
    email: EmailStr
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    authentication_method: Literal[
        UserAuthenticationMethod.GOOGLE_WORKSPACE,
        UserAuthenticationMethod.MICROSOFT_ENTRA,
    ]

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OrganisationMemberInvitationResponse(BaseModel):
    user_id: UUID
    email: EmailStr
