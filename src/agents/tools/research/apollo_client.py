"""Shared Apollo.io API client for people enrichment.

All LinkedIn enrichment tools delegate to this module. It wraps
Apollo's ``POST /people/match`` endpoint and normalizes the response
into a consistent structure.

API Docs: https://docs.apollo.io/reference/people-enrichment
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from src.infra.config.configs import apollo_config

logger = logging.getLogger(__name__)

API_TIMEOUT_SECONDS = 30


async def apollo_enrich_person(
    *,
    name: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    email: str | None = None,
    organization_name: str | None = None,
    domain: str | None = None,
    linkedin_url: str | None = None,
) -> dict[str, Any]:
    """Enrich a person via Apollo's POST /people/match.

    Provide as many identifiers as possible for best match accuracy.
    At minimum, provide one of: name+organization, email, or linkedin_url.

    Returns a normalized dict with keys:
        success, name, first_name, last_name, title, headline,
        linkedin_url, organization_name, location, employment_history,
        photo_url, apollo_id, raw (full API response)
    """
    api_key = await apollo_config.get_api_key()
    if not api_key:
        logger.warning("Apollo enrichment: API key not configured")
        return {"success": False, "error": "Apollo API key not configured"}

    body: dict[str, Any] = {}
    if name:
        body["name"] = name
    if first_name:
        body["first_name"] = first_name
    if last_name:
        body["last_name"] = last_name
    if email:
        body["email"] = email
    if organization_name:
        body["organization_name"] = organization_name
    if domain:
        body["domain"] = domain
    if linkedin_url:
        body["linkedin_url"] = linkedin_url

    if not body:
        return {"success": False, "error": "No search parameters provided"}

    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{apollo_config.APOLLO_BASE_URL}/people/match",
                json=body,
                headers={
                    "x-api-key": api_key,
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()

        return _normalize_response(data)

    except httpx.HTTPStatusError as e:
        error_msg = f"Apollo API error: {e.response.status_code}"
        if e.response.status_code == 401:
            error_msg = "Invalid Apollo API key"
        elif e.response.status_code == 422:
            error_msg = "Insufficient data to match a person"
        elif e.response.status_code == 429:
            error_msg = "Apollo rate limit exceeded"
        logger.warning("Apollo enrichment HTTP error: %s", error_msg)
        return {"success": False, "error": error_msg}
    except Exception as e:
        logger.exception("Apollo enrichment unexpected error: %s", e)
        return {"success": False, "error": str(e)}


def _normalize_response(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize Apollo /people/match response to a consistent structure.

    Apollo nests the person under 'person' key. If no match, person is None.
    """
    person = data.get("person")
    if not person:
        return {"success": False, "error": "No matching person found"}

    organization = person.get("organization") or {}
    employment_history = person.get("employment_history") or []

    return {
        "success": True,
        "apollo_id": person.get("id"),
        "name": person.get("name"),
        "first_name": person.get("first_name"),
        "last_name": person.get("last_name"),
        "title": person.get("title"),
        "headline": person.get("headline"),
        "linkedin_url": person.get("linkedin_url"),
        "photo_url": person.get("photo_url"),
        "location": ", ".join(
            filter(None, [person.get("city"), person.get("state"), person.get("country")])
        )
        or None,
        "organization_name": organization.get("name"),
        "organization_domain": organization.get("primary_domain"),
        "employment_history": _format_employment_history(employment_history),
        "raw": person,
    }


def _format_employment_history(
    history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Format Apollo employment_history for readability (max 5 entries)."""
    formatted = []
    for entry in history[:5]:
        if not isinstance(entry, dict):
            continue
        formatted.append(
            {
                "title": entry.get("title"),
                "company": entry.get("organization_name"),
                "start_date": entry.get("start_date"),
                "end_date": entry.get("end_date"),
                "current": entry.get("current", False),
            }
        )
    return formatted
