"""Public request and status schemas for provider-backed organisation signup."""

import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator
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
    """Collect profile data while excluding all server-owned authority fields."""

    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    job_title: str | None = Field(default=None, max_length=100)
    organisation_name: str = Field(min_length=2, max_length=255)
    primary_domain: str = Field(min_length=3, max_length=253)
    country: str = Field(min_length=2, max_length=2)
    sales_team_size: int | None = Field(default=None, ge=0, le=1_000_000)
    website_url: HttpUrl | None = None
    linkedin_url: HttpUrl | None = None
    terms_version: str = Field(min_length=1, max_length=32)
    privacy_version: str = Field(min_length=1, max_length=32)

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("primary_domain", mode="before")
    @classmethod
    def normalize_primary_domain(cls, value: object) -> object:
        """Normalize domains before length validation and persistence."""
        return normalize_signup_domain(value) if isinstance(value, str) else value

    @field_validator("country", mode="before")
    @classmethod
    def normalize_country(cls, value: object) -> object:
        """Store ISO-style country codes in one canonical form."""
        if not isinstance(value, str):
            return value
        country = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", country):
            raise ValueError("country must be a two-letter code")
        return country

    @model_validator(mode="after")
    def validate_website_domain(self) -> "ExternalSignupRequest":
        """Keep the claimed company website within the provider-verified domain."""
        if self.website_url is None:
            return self
        host = self.website_url.host
        if host is None or (
            host != self.primary_domain and not host.endswith(f".{self.primary_domain}")
        ):
            raise ValueError("website_url must use the primary domain")
        return self


class ExternalSignupResponse(BaseModel):
    """Expose only durable signup identifiers and provisioning status."""

    signup_request_id: UUID
    status: SignupRequestStatus
    organisation_id: UUID | None
    user_id: UUID | None

    model_config = ConfigDict(extra="forbid")
