"""HubSpot owner → NormalizedHubSpotOwner.

Pure functions, no I/O. Email normalisation is load-bearing for the match
step — we lowercase and strip before comparison on both sides.

Unparseable / malformed owners are filtered here (flagged via a skip
classification); anything that returns a normalised shape is guaranteed to
carry a usable `external_id`.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from src.infra.utils.logger import get_logger
from src.service.integrations.crm.records.hubspot.users.user_types import (
    NormalizedHubSpotOwner,
    OwnerMatchResult,
)

logger = get_logger(__name__)


def normalize_owners(
    raw_owners: Iterable[dict[str, Any]],
) -> tuple[list[NormalizedHubSpotOwner], int]:
    """Normalise every raw owner. Returns (valid list, skipped_malformed)."""
    normalized: list[NormalizedHubSpotOwner] = []
    skipped_malformed = 0
    for raw in raw_owners:
        owner = _normalize_one(raw)
        if owner is None:
            skipped_malformed += 1
            continue
        normalized.append(owner)
    return normalized, skipped_malformed


def resolve_matches(
    owners: Iterable[NormalizedHubSpotOwner],
    email_index: dict[str, Any],  # normalized_email → UUID (app user_id)
) -> list[OwnerMatchResult]:
    """Pair each normalised owner with an optional app-user match.

    `matched_app_user_id` is populated only when the owner's email is present
    in `email_index` (org-scoped). For owners with no email or no match, the
    writer is responsible for minting a fresh UUID — resolve_matches does not
    skip rows anymore.

    `email_index` is expected to already be normalised (lowercased, trimmed)
    on the KEY side.
    """
    results: list[OwnerMatchResult] = []
    for owner in owners:
        matched = email_index.get(owner.email) if owner.email is not None else None
        results.append(
            OwnerMatchResult(
                owner=owner,
                matched_app_user_id=matched,
            )
        )
    return results


def normalize_email(raw: Any) -> str | None:
    """Lowercase + strip, or None for anything non-string-or-empty."""
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().lower()
    return cleaned or None


def _normalize_one(raw: object) -> NormalizedHubSpotOwner | None:
    if not isinstance(raw, dict):
        logger.warning("Skipping non-dict HubSpot owner payload")
        return None

    external_id = _coerce_id(raw.get("id"))
    if external_id is None:
        logger.warning(
            "Skipping malformed HubSpot owner (no usable id)",
            extra={"raw_keys": list(raw.keys())},
        )
        return None

    # Defensive: /crm/v3/owners?archived=false should filter these out; catch
    # stragglers rather than persisting them.
    if raw.get("archived") is True:
        logger.warning(
            "Skipping archived HubSpot owner",
            extra={"owner_id": external_id},
        )
        return None

    first = _coerce_str(raw.get("firstName"))
    last = _coerce_str(raw.get("lastName"))
    email = normalize_email(raw.get("email"))
    full_name = _compose_full_name(first, last, email)
    user_role = _coerce_str(raw.get("type"))
    teams = raw.get("teams") or []
    if not isinstance(teams, list):
        teams = []
    team_ids, primary_team_id = _extract_teams(teams)
    source_created_at = _parse_iso(raw.get("createdAt"))
    source_modified_at = _parse_iso(raw.get("updatedAt"))

    hubspot_user_id = raw.get("userId")
    hubspot_user_id_including_inactive = raw.get("userIdIncludingInactive")
    properties: dict[str, Any] = {
        "hubspot_user_id": hubspot_user_id,
        "hubspot_user_id_including_inactive": hubspot_user_id_including_inactive,
        "hubspot_type": user_role,
        "teams": teams,
    }

    return NormalizedHubSpotOwner(
        external_id=external_id,
        email=email,
        first_name=first,
        last_name=last,
        full_name=full_name,
        user_role=user_role,
        team_ids=team_ids,
        primary_team_id=primary_team_id,
        source_created_at=source_created_at,
        source_modified_at=source_modified_at,
        properties=properties,
        data_payload=raw,
    )


def _coerce_id(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int):
        return str(value)
    return None


def _coerce_str(value: Any) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return None


def _compose_full_name(
    first: str | None,
    last: str | None,
    email_fallback: str | None,
) -> str | None:
    parts = [p for p in (first, last) if p]
    if parts:
        return " ".join(parts)
    return email_fallback


def _extract_teams(teams: list[Any]) -> tuple[tuple[str, ...], str | None]:
    seen: set[str] = set()
    ordered: list[str] = []
    primary: str | None = None
    for team in teams:
        if not isinstance(team, dict):
            continue
        tid = _coerce_id(team.get("id"))
        if tid is None or tid in seen:
            continue
        seen.add(tid)
        ordered.append(tid)
        if primary is None and team.get("primary") is True:
            primary = tid
    return tuple(ordered), primary


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
