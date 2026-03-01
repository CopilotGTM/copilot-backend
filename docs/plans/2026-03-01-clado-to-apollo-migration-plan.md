# Clado → Apollo LinkedIn Enrichment Migration Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace all Clado API calls with Apollo.io `POST /people/match` across three LinkedIn tools, pulling the API key from Azure Key Vault with TTL caching.

**Architecture:** Shared `apollo_client.py` module wraps the single Apollo endpoint. `ApolloConfig` gains Key Vault caching (same pattern as `TeamsConfig`/`SlackConfig`). All three tool files are rewritten to delegate to the shared client. `LinkedInEnrichmentConfig` and all Clado code are deleted.

**Tech Stack:** Python 3.11, httpx, Azure Key Vault (via `master_secrets_manager`), asyncio

---

### Task 1: Rewrite ApolloConfig with Key Vault caching + delete LinkedInEnrichmentConfig

**Files:**
- Modify: `src/infra/config/configs.py`

**Step 1: Rewrite `ApolloConfig` class (lines 122-136) to add Key Vault caching**

Replace the entire `ApolloConfig` class and delete `LinkedInEnrichmentConfig` (lines 138-142) plus the `linkedin_enrichment_config` singleton (line 519).

```python
class ApolloConfig:
    """Configuration for Apollo.io people enrichment.

    API key is stored in Azure Key Vault (master vault) and cached
    with a configurable TTL (default 6 hours). The first call to
    ``get_api_key()`` fetches from Key Vault; subsequent calls return
    from memory until the TTL expires.

    The key can also be set via env var (APOLLO_API_KEY) for local
    development — when present, Key Vault is skipped entirely.

    API Docs: https://docs.apollo.io/reference/introduction
    """

    APOLLO_BASE_URL: str = os.getenv("APOLLO_BASE_URL", "https://api.apollo.io/api/v1")

    # Key Vault secret name
    SECRET_NAME_API_KEY: str = "apollo-api-key"

    # Cached API key (populated by get_api_key)
    _api_key: str | None = os.getenv("APOLLO_API_KEY")

    # TTL for cached key (hours)
    _credentials_ttl_hours: float = float(os.getenv("APOLLO_CREDENTIALS_TTL_HOURS", "6"))
    _credentials_loaded_at: float | None = None
    _credentials_from_env: bool = bool(os.getenv("APOLLO_API_KEY"))
    _refresh_lock: asyncio.Lock | None = None

    async def get_api_key(self) -> str | None:
        """Return cached API key, refreshing from Key Vault if TTL expired."""
        if self._credentials_from_env:
            return self._api_key

        if not self._is_expired():
            return self._api_key

        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()

        async with self._refresh_lock:
            if not self._is_expired():
                return self._api_key
            await self._load_from_vault()

        return self._api_key

    def _is_expired(self) -> bool:
        if self._credentials_loaded_at is None:
            return True
        import time

        elapsed_hours = (time.time() - self._credentials_loaded_at) / 3600
        return elapsed_hours >= self._credentials_ttl_hours

    async def _load_from_vault(self) -> None:
        import time

        from src.infra.secrets.manager import master_secrets_manager

        logger.info("Loading Apollo API key from Key Vault (TTL refresh)")
        self._api_key = await master_secrets_manager.get(self.SECRET_NAME_API_KEY)
        self._credentials_loaded_at = time.time()

        if self._api_key:
            logger.info(
                "Apollo API key cached (TTL=%.1fh)", self._credentials_ttl_hours
            )
        else:
            logger.error("Apollo API key missing from Key Vault")
```

**Step 2: Delete `LinkedInEnrichmentConfig` class (lines 138-142)**

Remove:
```python
class LinkedInEnrichmentConfig:
    """Configuration for LinkedIn profile enrichment via Clado API."""

    LINKEDIN_CLADO_API_KEY: str | None = os.getenv("LINKEDIN_CLADO_API_KEY")
    CLADO_BASE_URL: str = os.getenv("CLADO_BASE_URL", "https://api.clado.ai")
```

**Step 3: Delete `linkedin_enrichment_config` singleton (line 519)**

Remove:
```python
linkedin_enrichment_config = LinkedInEnrichmentConfig()
```

The `apollo_config = ApolloConfig()` singleton on line 518 stays.

**Step 4: Commit**

```bash
git add src/infra/config/configs.py
git commit -m "feat(config): rewrite ApolloConfig with Key Vault caching, remove LinkedInEnrichmentConfig"
```

---

### Task 2: Create shared Apollo API client

**Files:**
- Create: `src/agents/tools/research/apollo_client.py`

**Step 1: Create the shared client module**

This is the single module all three tools will use. It wraps Apollo's `POST /people/match` endpoint and normalizes the response.

```python
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
        "location": (
            person.get("city")
            or person.get("state")
            or person.get("country")
        ),
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
```

**Step 2: Commit**

```bash
git add src/agents/tools/research/apollo_client.py
git commit -m "feat(tools): add shared Apollo API client for people enrichment"
```

---

### Task 3: Rewrite `enrich_stakeholder.py` to use Apollo

**Files:**
- Modify: `src/agents/tools/crm/enrich_stakeholder.py`

**Step 1: Complete rewrite of the file**

Key changes:
- Remove all `_clado_*` functions and `_normalize_search_result`
- Remove `httpx` import (no longer needed — `apollo_client` handles HTTP)
- Import `apollo_enrich_person` from `apollo_client`
- Replace the two-step flow (search → profile) with single `apollo_enrich_person()` call
- Change enrichment source from `"clado"` to `"apollo"`
- Keep unchanged: `_extract_linkedin_url`, `_is_recently_enriched`, `_build_enrichment_metadata`, `_infer_department`, factory function, security boundaries

```python
"""Stakeholder enrichment tool using Apollo.io API.

Enriches stakeholder records with LinkedIn profile data including:
- Full name, job title, department
- LinkedIn URL (stored in external_links)
- Work experience

Tracks enrichment status in stakeholder_data to avoid re-processing.

SECURITY BOUNDARIES (immutable, from context):
- organisation_id: Prevents cross-org data access
- deal_id: Optional - if provided, scopes to deal stakeholders
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

from sqlalchemy import select, update

from src.agents.tools.base import execute_with_capabilities
from src.agents.tools.research.apollo_client import apollo_enrich_person
from src.domain.models.organisation_tables.sync.crm.stakeholders import DealStakeholder

if TYPE_CHECKING:
    from src.agents.context import AgentContext
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)

ENRICHMENT_COOLDOWN_DAYS = 30


def _extract_linkedin_url(stakeholder: DealStakeholder) -> str | None:
    """Extract LinkedIn URL from stakeholder's external_links."""
    if not stakeholder.external_links:
        return None

    for link in stakeholder.external_links:
        if isinstance(link, dict) and link.get("platform", "").lower() == "linkedin":
            return link.get("url")

    return None


def _is_recently_enriched(stakeholder: DealStakeholder) -> bool:
    """Check if stakeholder was enriched recently."""
    if not stakeholder.stakeholder_data:
        return False

    enrichment = stakeholder.stakeholder_data.get("enrichment", {})
    enriched_at_str = enrichment.get("enriched_at")

    if not enriched_at_str:
        return False

    try:
        enriched_at = datetime.fromisoformat(enriched_at_str.replace("Z", "+00:00"))
        cooldown = timedelta(days=ENRICHMENT_COOLDOWN_DAYS)
        return datetime.now(UTC) - enriched_at < cooldown
    except Exception:
        return False


def _build_enrichment_metadata(
    linkedin_url: str | None,
    credits_used: int,
    raw_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build enrichment metadata for stakeholder_data."""
    metadata: dict[str, Any] = {
        "enriched_at": datetime.now(UTC).isoformat(),
        "enrichment_source": "apollo",
        "credits_used": credits_used,
    }
    if linkedin_url:
        metadata["linkedin_url"] = linkedin_url
    if raw_profile:
        metadata["profile_snapshot"] = {
            "name": raw_profile.get("name"),
            "headline": raw_profile.get("headline"),
            "title": raw_profile.get("title"),
            "location": raw_profile.get("location"),
        }
    return metadata


def _infer_department(job_title: str | None) -> str | None:
    """Infer department from job title."""
    if not job_title:
        return None

    title_lower = job_title.lower()

    department_keywords = {
        "Engineering": [
            "engineer", "developer", "architect", "cto", "technical", "devops", "sre",
        ],
        "Sales": ["sales", "account executive", "ae", "sdr", "bdr", "revenue"],
        "Marketing": ["marketing", "growth", "brand", "cmo", "demand gen"],
        "Product": ["product", "pm", "product manager", "product owner"],
        "Finance": ["finance", "cfo", "accounting", "controller", "treasurer"],
        "Legal": ["legal", "counsel", "attorney", "compliance", "general counsel"],
        "HR": ["hr", "human resources", "people", "talent", "recruiting"],
        "Operations": ["operations", "coo", "ops", "supply chain"],
        "IT": ["it ", "information technology", "infrastructure", "cio"],
        "Security": ["security", "ciso", "infosec", "cyber"],
        "Customer Success": ["customer success", "cs ", "csm", "support"],
        "Executive": ["ceo", "founder", "president", "chief executive"],
    }

    for department, keywords in department_keywords.items():
        if any(kw in title_lower for kw in keywords):
            return department

    return None


def _extract_domain_hint(email: str | None) -> str | None:
    """Extract company domain from email, skipping personal providers."""
    if not email or "@" not in email:
        return None
    domain = email.split("@")[1].lower()
    personal = (".gmail.com", ".yahoo.com", ".hotmail.com", ".outlook.com")
    if domain.endswith(personal):
        return None
    return domain


async def enrich_stakeholder(
    *,
    context: AgentContext,
    stakeholder_id: str | None = None,
    email: str | None = None,
    name: str | None = None,
    company_name: str | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Enrich a stakeholder with LinkedIn profile data via Apollo API."""
    organisation_id = context.organisation_id
    deal_id = context.deal_id

    if not organisation_id:
        logger.warning("enrich_stakeholder: missing organisation_id")
        return {"success": False, "error": "Missing organisation_id"}

    if not stakeholder_id and not email:
        return {"success": False, "error": "Must provide stakeholder_id or email"}

    c = cast(Any, DealStakeholder).__table__.c

    async with context.acquire_session() as session:
        try:
            # Find stakeholder
            if stakeholder_id:
                stmt = select(DealStakeholder).where(
                    c.organisation_id == organisation_id,
                    c.stakeholder_id == UUID(stakeholder_id),
                )
            else:
                assert email is not None
                stmt = select(DealStakeholder).where(
                    c.organisation_id == organisation_id,
                    c.email == email.lower().strip(),
                )
                if deal_id:
                    stmt = stmt.where(c.deal_id == deal_id)

            result = await session.execute(stmt)
            stakeholder = result.scalar_one_or_none()

            if not stakeholder:
                return {
                    "success": False,
                    "error": "Stakeholder not found",
                    "stakeholder_id": stakeholder_id,
                    "email": email,
                }

            # Check if recently enriched
            if not force_refresh and _is_recently_enriched(stakeholder):
                logger.info(
                    "enrich_stakeholder: skipping recently enriched stakeholder %s",
                    stakeholder.stakeholder_id,
                )
                existing_enrichment = (stakeholder.stakeholder_data or {}).get(
                    "enrichment", {}
                )
                return {
                    "success": True,
                    "stakeholder_id": str(stakeholder.stakeholder_id),
                    "already_enriched": True,
                    "enriched_at": existing_enrichment.get("enriched_at"),
                    "data": {
                        "name": stakeholder.name,
                        "job_title": stakeholder.job_title,
                        "department": stakeholder.department,
                        "linkedin_url": _extract_linkedin_url(stakeholder),
                    },
                }

            # Build search params for Apollo
            search_email = email or stakeholder.email
            search_name = name or stakeholder.name
            search_company = company_name
            linkedin_url = _extract_linkedin_url(stakeholder)
            domain_hint = _extract_domain_hint(search_email)

            # Single Apollo call replaces Clado's search + profile steps
            apollo_result = await apollo_enrich_person(
                name=search_name,
                email=search_email,
                organization_name=search_company,
                domain=domain_hint,
                linkedin_url=linkedin_url,
            )

            credits_used = 1  # Apollo charges 1 credit per /people/match call
            profile_data = apollo_result if apollo_result.get("success") else None

            if profile_data:
                linkedin_url = profile_data.get("linkedin_url") or linkedin_url

            # Prepare updates
            updates: dict[str, Any] = {}

            if profile_data:
                profile_name = profile_data.get("name")
                if profile_name and (
                    not stakeholder.name
                    or len(profile_name) > len(stakeholder.name)
                ):
                    updates["name"] = profile_name

                profile_title = (
                    profile_data.get("title") or profile_data.get("headline")
                )
                if profile_title:
                    updates["job_title"] = profile_title

                if not stakeholder.department:
                    department = _infer_department(profile_title)
                    if department:
                        updates["department"] = department

            # Update external_links with LinkedIn URL
            if linkedin_url:
                existing_links = stakeholder.external_links or []
                has_linkedin = any(
                    isinstance(link, dict)
                    and link.get("platform", "").lower() == "linkedin"
                    for link in existing_links
                )
                if not has_linkedin:
                    new_link = {
                        "platform": "linkedin",
                        "url": linkedin_url,
                        "label": "LinkedIn",
                        "added_at": datetime.now(UTC).isoformat(),
                    }
                    updates["external_links"] = existing_links + [new_link]

            # Update enrichment metadata
            existing_data = stakeholder.stakeholder_data or {}
            existing_data["enrichment"] = _build_enrichment_metadata(
                linkedin_url=linkedin_url,
                credits_used=credits_used,
                raw_profile=profile_data,
            )
            updates["stakeholder_data"] = existing_data
            updates["updated_at"] = datetime.now(UTC)

            if updates:
                update_stmt = (
                    update(DealStakeholder)
                    .where(c.stakeholder_id == stakeholder.stakeholder_id)
                    .values(**updates)
                )
                await session.execute(update_stmt)
                await session.commit()

            logger.info(
                "enrich_stakeholder: enriched stakeholder %s, credits_used=%d",
                stakeholder.stakeholder_id,
                credits_used,
            )

            return {
                "success": True,
                "stakeholder_id": str(stakeholder.stakeholder_id),
                "already_enriched": False,
                "data": {
                    "name": updates.get("name", stakeholder.name),
                    "job_title": updates.get("job_title", stakeholder.job_title),
                    "department": updates.get(
                        "department", stakeholder.department
                    ),
                    "linkedin_url": linkedin_url,
                },
                "metadata": {
                    "enriched_at": datetime.now(UTC).isoformat(),
                    "enrichment_source": "apollo",
                    "credits_used": credits_used,
                    "profile_found": profile_data is not None,
                },
            }

        except Exception as e:
            logger.exception("enrich_stakeholder failed: %s", e)
            await session.rollback()
            return {"success": False, "error": str(e)}


def create_enrich_stakeholder(
    context: AgentContext,
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Any]:
    """Factory to create the enrich_stakeholder tool bound to a context.

    SECURITY BOUNDARIES (from context, immutable):
    - organisation_id: Prevents cross-org data access
    - deal_id: Optional scope to deal stakeholders
    """

    async def enrich_stakeholder_tool(
        stakeholder_id: str | None = None,
        email: str | None = None,
        name: str | None = None,
        company_name: str | None = None,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        """Enrich a stakeholder with LinkedIn profile data.

        Use this tool to get detailed professional information about a stakeholder
        from LinkedIn. The tool will find their LinkedIn profile, extract relevant
        data (job title, department), and update the stakeholder record.

        The tool tracks enrichment status - if a stakeholder was enriched recently
        (within 30 days), it returns cached data unless force_refresh=True.

        Args:
            stakeholder_id: UUID of the stakeholder to enrich (preferred method)
            email: Email address to find the stakeholder (if stakeholder_id not provided)
            name: Full name to help find LinkedIn profile (optional, improves accuracy)
            company_name: Company name to help find LinkedIn profile (optional)
            force_refresh: Set to True to re-enrich even if recently enriched

        Returns:
            Dictionary containing:
            - success: Whether enrichment succeeded
            - stakeholder_id: UUID of the stakeholder
            - already_enriched: True if returning cached data
            - data: Enriched profile data (name, job_title, department, linkedin_url)
            - metadata: Enrichment details (source, credits used, timestamp)
        """
        logger.info(
            "[TOOL CALLED] enrich_stakeholder - stakeholder_id=%s, email=%s",
            stakeholder_id,
            email,
        )

        if not context.organisation_id:
            logger.warning("enrich_stakeholder unavailable: missing organisation_id")
            return {"success": False, "error": "Missing organisation context"}

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="EnrichStakeholder",
            tool_func=enrich_stakeholder,
            parameters={
                "context": context,
                "stakeholder_id": stakeholder_id,
                "email": email,
                "name": name,
                "company_name": company_name,
                "force_refresh": force_refresh,
            },
            description=f"Enriching stakeholder: {stakeholder_id or email}",
            fallback_value={"success": False, "error": "Tool execution failed"},
        )

        if result.get("success"):
            logger.info(
                "[TOOL RESULT] enrich_stakeholder success: stakeholder=%s, credits=%s",
                result.get("stakeholder_id"),
                result.get("metadata", {}).get("credits_used", 0),
            )
        else:
            logger.info(
                "[TOOL RESULT] enrich_stakeholder failed: %s",
                result.get("error", "unknown"),
            )

        return dict(result) if result else {"success": False, "error": "No result"}

    return enrich_stakeholder_tool


__all__ = [
    "enrich_stakeholder",
    "create_enrich_stakeholder",
]
```

**Step 2: Commit**

```bash
git add src/agents/tools/crm/enrich_stakeholder.py
git commit -m "feat(tools): rewrite enrich_stakeholder to use Apollo API"
```

---

### Task 4: Rewrite `linkedin_enrichment.py` to use Apollo

**Files:**
- Modify: `src/agents/tools/research/linkedin_enrichment.py`

**Step 1: Complete rewrite**

Key changes:
- Remove all Clado functions: `_make_clado_request`, `get_linkedin_profile`, `scrape_linkedin_profile`, `get_linkedin_contact_info`
- Remove `fresh_scrape` and `include_contact` parameters
- Remove `httpx` import
- Keep `_validate_linkedin_url` (still useful)
- Delegate to `apollo_enrich_person` from `apollo_client`

```python
"""LinkedIn enrichment tool using Apollo.io API.

Provides LinkedIn profile enrichment for stakeholder research.
API key is sourced from Azure Key Vault via ApolloConfig.

API Documentation: https://docs.apollo.io/reference/people-enrichment
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from src.agents.tools.base import execute_with_capabilities
from src.agents.tools.research.apollo_client import apollo_enrich_person

if TYPE_CHECKING:
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


def _validate_linkedin_url(url: str) -> str | None:
    """Validate and normalize a LinkedIn URL."""
    if not url:
        return None

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        parsed = urlparse(url)
        if "linkedin.com" not in parsed.netloc.lower():
            return None
        if not parsed.path or parsed.path == "/":
            return None
        return url
    except Exception:
        return None


async def enrich_linkedin_profile(
    linkedin_url: str,
) -> dict[str, Any]:
    """Enrich a LinkedIn profile via Apollo API.

    Args:
        linkedin_url: LinkedIn profile URL

    Returns:
        Dictionary with profile data or error information
    """
    validated_url = _validate_linkedin_url(linkedin_url)
    if not validated_url:
        logger.warning("LinkedIn enrichment: invalid LinkedIn URL: %s", linkedin_url)
        return {"error": "Invalid LinkedIn URL", "success": False}

    result = await apollo_enrich_person(linkedin_url=validated_url)

    if not result.get("success"):
        return result

    return {
        "success": True,
        "profile": result,
        "source": "apollo",
        "credits_used": 1,
    }


def create_enrich_linkedin_profile(
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Factory that returns a LinkedIn enrichment tool function.

    This tool doesn't require AgentContext since it calls
    an external API and doesn't access organisation data directly.
    """

    async def enrich_linkedin_profile_tool(
        linkedin_url: str,
    ) -> dict[str, Any]:
        """Enrich a LinkedIn profile with detailed professional information.

        Use this tool to get detailed information about a person from their
        LinkedIn profile, including their current role and work history.
        Useful for stakeholder research and understanding contacts.

        Args:
            linkedin_url: LinkedIn profile URL (e.g., "https://linkedin.com/in/john-smith"
                         or just "linkedin.com/in/john-smith")

        Returns:
            Dictionary containing:
            - success: Whether the enrichment succeeded
            - profile: Profile data including name, headline, title, location,
              employment_history, linkedin_url, organization_name
            - credits_used: API credits consumed (1 per call)
            - error: Error message if failed
        """
        logger.info(
            "[TOOL CALLED] enrich_linkedin_profile - url='%s'",
            linkedin_url,
        )

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="EnrichLinkedInProfile",
            tool_func=enrich_linkedin_profile,
            parameters={"linkedin_url": linkedin_url},
            description=f"Enriching LinkedIn profile: {linkedin_url}",
            fallback_value={"error": "Tool execution failed", "success": False},
        )

        if result.get("success"):
            profile = result.get("profile", {})
            logger.info(
                "[TOOL RESULT] enrich_linkedin_profile success: name=%s",
                profile.get("name", "unknown"),
            )
        else:
            logger.info(
                "[TOOL RESULT] enrich_linkedin_profile failed: %s",
                result.get("error", "unknown error"),
            )

        return dict(result) if result else {}

    return enrich_linkedin_profile_tool


__all__ = [
    "enrich_linkedin_profile",
    "create_enrich_linkedin_profile",
]
```

**Step 2: Update `src/agents/tools/research/__init__.py`**

Remove the old exports that no longer exist: `get_linkedin_profile`, `scrape_linkedin_profile`, `get_linkedin_contact_info`. The `__init__.py` imports from this module — update accordingly. Only `enrich_linkedin_profile` and `create_enrich_linkedin_profile` remain.

**Step 3: Commit**

```bash
git add src/agents/tools/research/linkedin_enrichment.py src/agents/tools/research/__init__.py
git commit -m "feat(tools): rewrite linkedin_enrichment to use Apollo API"
```

---

### Task 5: Rewrite `research_linkedin_profile.py` to use Apollo

**Files:**
- Modify: `src/agents/tools/research/research_linkedin_profile.py`

**Step 1: Complete rewrite**

Key changes:
- Remove `_search_linkedin`, `_get_linkedin_profile`, `_format_experience`, `_format_education`
- Remove `httpx` import
- Delegate to `apollo_enrich_person` for both URL and name search modes
- Keep `_is_linkedin_url` and `_normalize_linkedin_url`

```python
"""LinkedIn profile research tool for general chat.

This tool allows agents to research LinkedIn profiles without updating any database.
It's designed for general research queries like "tell me about this person on LinkedIn".

Unlike enrich_stakeholder, this tool:
- Does NOT require organisation context
- Does NOT update any database records
- Simply returns LinkedIn profile information for research purposes
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from src.agents.tools.base import execute_with_capabilities
from src.agents.tools.research.apollo_client import apollo_enrich_person

if TYPE_CHECKING:
    from src.agents.tools.transparent_tools import AgentCapabilities

logger = logging.getLogger(__name__)


def _is_linkedin_url(text: str) -> bool:
    """Check if the text is a LinkedIn URL."""
    if not text:
        return False
    text = text.strip().lower()
    return "linkedin.com/in/" in text or "linkedin.com/company/" in text


def _normalize_linkedin_url(url: str) -> str | None:
    """Normalize a LinkedIn URL."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        parsed = urlparse(url)
        if "linkedin.com" not in parsed.netloc.lower():
            return None
        if not parsed.path or parsed.path == "/":
            return None
        return url
    except Exception:
        return None


async def research_linkedin_profile(
    query: str,
    company: str | None = None,
) -> dict[str, Any]:
    """Research a LinkedIn profile by name or URL via Apollo API.

    Args:
        query: Either a LinkedIn URL or a person's name to search for
        company: Optional company name to narrow search (only used if query is a name)

    Returns:
        Dictionary with profile research results
    """
    # Mode 1: Direct LinkedIn URL
    if _is_linkedin_url(query):
        linkedin_url = _normalize_linkedin_url(query)
        if not linkedin_url:
            return {"success": False, "error": "Invalid LinkedIn URL format"}

        result = await apollo_enrich_person(linkedin_url=linkedin_url)
        if not result.get("success"):
            return result

        return {
            "success": True,
            "linkedin_url": result.get("linkedin_url") or linkedin_url,
            "profile": result,
        }

    # Mode 2: Search by name (+optional company)
    result = await apollo_enrich_person(
        name=query,
        organization_name=company,
    )

    if not result.get("success"):
        return {
            "success": False,
            "error": f"Could not find LinkedIn profile for '{query}'",
            "suggestion": (
                "Try providing the LinkedIn URL directly or a more specific name"
            ),
        }

    return {
        "success": True,
        "linkedin_url": result.get("linkedin_url"),
        "profile": result,
    }


def create_research_linkedin_profile(
    capabilities_getter: Callable[[], AgentCapabilities | None],
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Factory that creates the LinkedIn research tool.

    This tool is for general research purposes and does NOT:
    - Require organisation context
    - Update any database records
    """

    async def research_linkedin_profile_tool(
        query: str,
        company: str | None = None,
    ) -> dict[str, Any]:
        """Research a LinkedIn profile to learn about a person.

        Use this tool when someone asks about a person's professional background,
        or wants to know more about someone on LinkedIn. You can provide either
        a LinkedIn URL or the person's name.

        Args:
            query: Either a LinkedIn profile URL (e.g., "https://linkedin.com/in/john-smith")
                   OR a person's name to search for (e.g., "John Smith")
            company: Optional company name to help find the right person.
                    Only used when query is a name, not a URL.
                    Example: "Acme Corp" or "Google"

        Returns:
            Dictionary containing:
            - success: Whether the research succeeded
            - linkedin_url: The person's LinkedIn profile URL
            - profile: Profile information including name, headline,
              title, location, employment_history, organization_name
            - error: Error message if failed
        """
        logger.info(
            "[TOOL CALLED] research_linkedin_profile - query='%s', company='%s'",
            query,
            company,
        )

        result = await execute_with_capabilities(
            capabilities_getter(),
            tool_name="ResearchLinkedInProfile",
            tool_func=research_linkedin_profile,
            parameters={"query": query, "company": company},
            description=f"Researching LinkedIn profile: {query}",
            fallback_value={"success": False, "error": "Tool execution failed"},
        )

        if result.get("success"):
            profile = result.get("profile", {})
            logger.info(
                "[TOOL RESULT] research_linkedin_profile success: name=%s",
                profile.get("name", "unknown"),
            )
        else:
            logger.info(
                "[TOOL RESULT] research_linkedin_profile failed: %s",
                result.get("error", "unknown"),
            )

        return dict(result) if result else {}

    return research_linkedin_profile_tool


__all__ = [
    "research_linkedin_profile",
    "create_research_linkedin_profile",
]
```

**Step 2: Commit**

```bash
git add src/agents/tools/research/research_linkedin_profile.py
git commit -m "feat(tools): rewrite research_linkedin_profile to use Apollo API"
```

---

### Task 6: Update `research/__init__.py` exports

**Files:**
- Modify: `src/agents/tools/research/__init__.py`

**Step 1: Update imports**

Remove old Clado-specific exports that no longer exist (`get_linkedin_profile`, `scrape_linkedin_profile`, `get_linkedin_contact_info`). Add export of the new `apollo_client` module. Keep `enrich_linkedin_profile`, `create_enrich_linkedin_profile`, `research_linkedin_profile`, `create_research_linkedin_profile`.

**Step 2: Commit**

```bash
git add src/agents/tools/research/__init__.py
git commit -m "refactor(tools): update research __init__ exports for Apollo migration"
```

---

### Task 7: Update test script

**Files:**
- Modify: `scripts/maintenance/test_enrich_stakeholder.py`

**Step 1: Rewrite test script to test Apollo API**

Replace all `_clado_*` function calls with `apollo_enrich_person`. Replace `linkedin_enrichment_config` references with `apollo_config`. Remove contact lookup test. Update print messages from "Clado" to "Apollo".

Key changes:
- `test_api_key_config()`: Check `apollo_config.get_api_key()` instead of `LINKEDIN_CLADO_API_KEY`
- `test_clado_api_search()` → `test_apollo_search()`: Use `apollo_enrich_person(name=..., organization_name=...)`
- `test_clado_api_profile()` → `test_apollo_profile()`: Use `apollo_enrich_person(linkedin_url=...)`
- Remove `test_clado_api_contact()` entirely
- Update `test_full_enrichment_workflow()` to use the new functions
- `test_security_boundaries()` and `test_department_inference()` stay the same (they don't touch the API)

**Step 2: Commit**

```bash
git add scripts/maintenance/test_enrich_stakeholder.py
git commit -m "test(tools): update enrichment test script for Apollo API"
```

---

### Task 8: Verify no remaining Clado references

**Step 1: Search for any remaining Clado references**

Run: `grep -ri "clado\|LINKEDIN_CLADO\|linkedin_enrichment_config" src/ scripts/`

Expected: No matches

**Step 2: Verify imports work**

Run: `uv run python -c "from src.agents.tools.research.apollo_client import apollo_enrich_person; print('OK')"`

Run: `uv run python -c "from src.agents.tools.crm.enrich_stakeholder import create_enrich_stakeholder; print('OK')"`

Run: `uv run python -c "from src.agents.tools.research.linkedin_enrichment import create_enrich_linkedin_profile; print('OK')"`

Run: `uv run python -c "from src.agents.tools.research.research_linkedin_profile import create_research_linkedin_profile; print('OK')"`

Expected: All print "OK"

**Step 3: Run linting**

Run: `uv run ruff check src/agents/tools/crm/enrich_stakeholder.py src/agents/tools/research/linkedin_enrichment.py src/agents/tools/research/research_linkedin_profile.py src/agents/tools/research/apollo_client.py src/infra/config/configs.py`

Fix any issues.

**Step 4: Final commit**

```bash
git commit -m "chore: verify Clado fully removed, all Apollo imports working"
```
