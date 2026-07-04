"""Salesforce User -> NormalizedSalesforceUser."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any
from uuid import UUID

from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.salesforce.users.user_types import (
    NormalizedSalesforceUser,
    UserMatchResult,
)

logger = get_logger(__name__)


def normalize_users(
    raw_users: Iterable[dict[str, Any]],
) -> tuple[list[NormalizedSalesforceUser], int, int]:
    """Normalize active Salesforce users and count malformed/inactive skips.

    Salesforce sync treats `IsActive=false` users as out-of-scope so onboarding
    a new org cannot create CRM identities for former employees.
    """
    normalized: list[NormalizedSalesforceUser] = []
    skipped_malformed = 0
    skipped_inactive = 0
    for raw in raw_users:
        user, skip_reason = _normalize_one(raw)
        if user is None:
            if skip_reason == "inactive":
                skipped_inactive += 1
            else:
                skipped_malformed += 1
            continue
        normalized.append(user)
    return normalized, skipped_malformed, skipped_inactive


def resolve_matches(
    users: Iterable[NormalizedSalesforceUser],
    email_index: dict[str, Any],
) -> list[UserMatchResult]:
    """Attach an optional app-user UUID by normalized email.

    The email index is org-scoped by the caller; matching here must not reach
    across organisations.
    """
    results: list[UserMatchResult] = []
    for user in users:
        matched = email_index.get(user.email) if user.email is not None else None
        results.append(UserMatchResult(user=user, matched_app_user_id=matched))
    return results


def build_owner_backfill_mapping(
    *,
    users: Iterable[NormalizedSalesforceUser],
    user_pk_map: dict[str, UUID],
) -> tuple[
    dict[str, tuple[UUID, str | None, None, str | None]], dict[str, tuple[UUID, None, None, None]]
]:
    """Build provider-id to local-user mappings for owner FK backfills."""
    owner_mapping: dict[str, tuple[UUID, str | None, None, str | None]] = {}
    fk_only_mapping: dict[str, tuple[UUID, None, None, None]] = {}
    for user in users:
        pk = user_pk_map.get(user.external_id)
        if pk is None:
            continue
        owner_mapping[user.external_id] = (pk, user.full_name, None, user.email)
        fk_only_mapping[user.external_id] = (pk, None, None, None)
    return owner_mapping, fk_only_mapping


def normalize_email(raw: Any) -> str | None:
    """Return the canonical email key used for app-user and CRM-user matching."""
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().lower()
    return cleaned or None


def _normalize_one(raw: object) -> tuple[NormalizedSalesforceUser | None, str | None]:
    if not isinstance(raw, dict):
        logger.warning("Skipping non-dict Salesforce user payload")
        return None, "malformed"

    external_id = _coerce_id(raw.get("Id"))
    if external_id is None:
        logger.warning(
            "Skipping malformed Salesforce user (no usable Id)",
            extra={"raw_keys": list(raw.keys())},
        )
        return None, "malformed"
    if not _coerce_bool(raw.get("IsActive")):
        logger.info(
            "Skipping inactive Salesforce user",
            extra={"user_external_id": external_id},
        )
        return None, "inactive"

    first_name = _coerce_str(raw.get("FirstName"))
    last_name = _coerce_str(raw.get("LastName"))
    email = normalize_email(raw.get("Email"))
    full_name = _coerce_str(raw.get("Name")) or _compose_full_name(first_name, last_name, email)
    user_role = _extract_profile_name(raw)
    profile_id = _coerce_str(raw.get("ProfileId"))
    user_type = _coerce_str(raw.get("UserType"))

    return NormalizedSalesforceUser(
        external_id=external_id,
        email=email,
        first_name=first_name,
        last_name=last_name,
        full_name=full_name,
        user_role=user_role,
        is_active=True,
        source_created_at=_parse_salesforce_timestamp(raw.get("CreatedDate")),
        source_modified_at=_parse_salesforce_timestamp(raw.get("LastModifiedDate")),
        properties={
            "salesforce_profile_id": profile_id,
            "salesforce_profile_name": user_role,
            "salesforce_user_type": user_type,
        },
        data_payload=raw,
    ), None


def _extract_profile_name(raw: dict[str, Any]) -> str | None:
    profile = raw.get("Profile")
    if isinstance(profile, dict):
        return _coerce_str(profile.get("Name"))
    return _coerce_str(raw.get("Profile.Name"))


def _coerce_id(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _coerce_str(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    if isinstance(value, (int, float)):
        return value != 0
    return False


def _compose_full_name(
    first_name: str | None,
    last_name: str | None,
    email_fallback: str | None,
) -> str | None:
    parts = [p for p in (first_name, last_name) if p]
    if parts:
        return " ".join(parts)
    return email_fallback


def _parse_salesforce_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    cleaned = value
    if len(cleaned) >= 5 and cleaned[-5] in {"+", "-"} and cleaned[-3] != ":":
        cleaned = f"{cleaned[:-2]}:{cleaned[-2:]}"
    try:
        return datetime.fromisoformat(cleaned)
    except ValueError:
        return None
