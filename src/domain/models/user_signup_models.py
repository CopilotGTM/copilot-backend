"""Public request and status schemas for provider-backed organisation signup."""

import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from src.domain.models.master_tables import SignupRequestStatus


def normalize_signup_domain(value: str) -> str:
    """Normalize a bare DNS domain to its lowercase ASCII representation."""
    domain = value.strip().lower().rstrip(".")
    if "://" in domain or "/" in domain or "@" in domain:
        raise ValueError("primary_domain must be a bare domain")
    try:
        normalized = domain.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("primary_domain is invalid") from exc
    labels = normalized.split(".")
    if len(labels) < 2 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels
    ):
        raise ValueError("primary_domain is invalid")
    return normalized


class ExternalSignupRequest(BaseModel):
    """Accept only the workspace name the verified founder is allowed to choose."""

    organisation_name: str = Field(min_length=2, max_length=255)

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ExternalSignupProfile(BaseModel):
    """Return provider identity and the server-side draft used to resume signup."""

    signup_request_id: UUID
    status: SignupRequestStatus
    provider: str
    email: str
    primary_domain: str
    full_name: str | None
    profile_image_url: str | None
    organisation_name: str | None

    model_config = ConfigDict(extra="forbid")


class ExternalSignupResponse(BaseModel):
    """Expose only durable signup identifiers and provisioning status."""

    signup_request_id: UUID
    status: SignupRequestStatus
    organisation_id: UUID | None
    user_id: UUID | None

    model_config = ConfigDict(extra="forbid")
