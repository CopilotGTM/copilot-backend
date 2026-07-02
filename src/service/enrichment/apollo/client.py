"""Apollo.io API client for people and organization enrichment.

CRM services and agent tools both call this module. It owns the provider API
boundary and returns normalized dictionaries so callers do not depend on
Apollo's raw response shape.
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
    """Enrich a person via Apollo's POST /people/match."""
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


async def apollo_enrich_organization(
    *,
    domain: str | None = None,
    name: str | None = None,
    website: str | None = None,
    linkedin_url: str | None = None,
) -> dict[str, Any]:
    """Enrich one company via Apollo's GET /organizations/enrich."""
    api_key = await apollo_config.get_api_key()
    if not api_key:
        logger.warning("Apollo organization enrichment: API key not configured")
        return {"success": False, "error": "Apollo API key not configured"}

    params = {
        key: value
        for key, value in {
            "domain": domain,
            "name": name,
            "website": website,
            "linkedin_url": linkedin_url,
        }.items()
        if value
    }
    if not params:
        return {"success": False, "error": "No organization search parameters provided"}

    try:
        async with httpx.AsyncClient(timeout=API_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{apollo_config.APOLLO_BASE_URL}/organizations/enrich",
                params=params,
                headers={
                    "x-api-key": api_key,
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            data: dict[str, Any] = response.json()

        return _normalize_organization_response(data)

    except httpx.HTTPStatusError as e:
        error_msg = f"Apollo organization API error: {e.response.status_code}"
        if e.response.status_code == 401:
            error_msg = "Invalid Apollo API key"
        elif e.response.status_code == 422:
            error_msg = "Insufficient data to match an organization"
        elif e.response.status_code == 429:
            error_msg = "Apollo rate limit exceeded"
        logger.warning("Apollo organization enrichment HTTP error: %s", error_msg)
        return {"success": False, "error": error_msg}
    except Exception as e:
        logger.exception("Apollo organization enrichment unexpected error: %s", e)
        return {"success": False, "error": str(e)}


def _normalize_response(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize Apollo /people/match response to a consistent structure."""
    person = data.get("person")
    if not person:
        return {"success": False, "error": "No matching person found"}

    organization = person.get("organization") or {}
    employment_history = person.get("employment_history") or []

    return {
        "success": True,
        "apollo_id": person.get("id"),
        "email": person.get("email"),
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


def _normalize_organization_response(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize Apollo organization enrichment response."""
    organization = data.get("organization") or data.get("account")
    if not organization:
        return {"success": False, "error": "No matching organization found"}

    primary_phone = organization.get("primary_phone")
    primary_phone_number = primary_phone.get("number") if isinstance(primary_phone, dict) else None

    return {
        "success": True,
        "apollo_id": organization.get("id"),
        "name": organization.get("name"),
        "domain": organization.get("primary_domain") or organization.get("domain"),
        "website": organization.get("website_url") or organization.get("website"),
        "linkedin_url": organization.get("linkedin_url"),
        "industry": organization.get("industry"),
        "keywords": organization.get("keywords") or [],
        "technologies": organization.get("technologies") or [],
        "number_of_employees": (
            organization.get("estimated_num_employees")
            or organization.get("num_employees")
            or organization.get("employee_count")
        ),
        "annual_revenue": (
            organization.get("annual_revenue")
            or organization.get("estimated_annual_revenue")
            or organization.get("revenue")
        ),
        "phone": organization.get("phone") or primary_phone_number,
        "city": organization.get("city"),
        "state": organization.get("state"),
        "country": organization.get("country"),
        "address": organization.get("raw_address") or organization.get("address"),
        "postal_code": organization.get("postal_code"),
        "founded_year": organization.get("founded_year"),
        "logo_url": organization.get("logo_url"),
        "raw": organization,
    }


def _format_employment_history(
    history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Format Apollo employment_history for readability."""
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
