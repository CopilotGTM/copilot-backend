"""Salesforce CRM provider.

This module provides registry-compatible wrapper functions which delegate to
Salesforce-specific fetchers (SOQL via Nango proxy).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src.infra.utils.logger import get_logger
from src.service.integrations.nango_client import NangoClient
from src.workers.integrations_worker.utils.sync_metadata import (
    SyncContext,
    classify_records,
    log_sync_action_distribution,
)

from .fetchers import (
    SalesforceAccountFetcher,
    SalesforceCaseFetcher,
    SalesforceContactFetcher,
    SalesforceContactRoleFetcher,
    SalesforceLeadFetcher,
    SalesforceNoteFetcher,
    SalesforceOpportunityFetcher,
    SalesforcePipelineFetcher,
    SalesforceTaskFetcher,
    SalesforceUserFetcher,
)

logger = get_logger(__name__)

__all__ = [
    "fetch_accounts",
    "fetch_companies",
    "fetch_contacts",
    "fetch_contact_roles",
    "fetch_deals",
    "fetch_opportunities",
    "fetch_leads",
    "fetch_users",
    "fetch_tasks",
    "fetch_notes",
    "fetch_tickets",
    "fetch_pipelines",
]


def _sf_to_canonical_note(raw: dict[str, Any]) -> dict[str, Any]:
    """Transform a Salesforce Note into the canonical activity payload.

    We map ParentId -> what_id so the existing DealTaskSyncService logic links
    Opportunity notes (ParentId prefix 006) to deals.
    """

    owner_data = raw.get("Owner") or {}
    owner_name = owner_data.get("Name") if isinstance(owner_data, dict) else None
    owner_email = owner_data.get("Email") if isinstance(owner_data, dict) else None

    props: dict[str, Any] = {
        "subject": raw.get("Title"),
        "description": raw.get("Body"),
        "owner_id": raw.get("OwnerId"),
        "hubspot_owner_id": raw.get("OwnerId"),
        "hubspot_owner_name": owner_name,
        "hubspot_owner_email": owner_email,
        # Linkage: treat ParentId like WhatId for activities.
        "what_id": raw.get("ParentId"),
        # Date fields expected by DealTaskSyncService
        "created_date": _as_iso(raw.get("CreatedDate")),
        "last_modified_date": _as_iso(raw.get("LastModifiedDate")),
        # Force activity type normalization to NOTE.
        "type": "NOTE",
    }

    return {
        "id": raw.get("Id"),
        "createdAt": _as_iso(raw.get("CreatedDate")),
        "updatedAt": _as_iso(raw.get("LastModifiedDate")),
        "archived": bool(raw.get("IsDeleted")),
        "properties": {k: v for k, v in props.items() if v is not None},
        "associations": {},
        "data_payload": raw,
    }


def _as_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _sf_to_canonical_deal(raw: dict[str, Any]) -> dict[str, Any]:
    is_closed = raw.get("IsClosed")
    is_won = raw.get("IsWon")
    is_closed_lost: bool | None
    if isinstance(is_closed, bool) and isinstance(is_won, bool):
        is_closed_lost = is_closed and (not is_won)
    else:
        is_closed_lost = None

    # Extract owner details from relationship query (Owner.Name, Owner.Email, etc.)
    owner_data = raw.get("Owner") or {}
    owner_name = owner_data.get("Name") if isinstance(owner_data, dict) else None
    owner_email = owner_data.get("Email") if isinstance(owner_data, dict) else None
    owner_first_name = owner_data.get("FirstName") if isinstance(owner_data, dict) else None
    owner_last_name = owner_data.get("LastName") if isinstance(owner_data, dict) else None

    props: dict[str, Any] = {
        "dealname": raw.get("Name"),
        "amount": raw.get("Amount"),
        "deal_currency_code": raw.get("CurrencyIsoCode"),
        "pipeline": "salesforce_opportunity",
        "dealstage": raw.get("StageName"),
        "closedate": raw.get("CloseDate"),
        "hubspot_owner_id": raw.get("OwnerId"),
        "accountid": raw.get("AccountId"),
        "hs_lastmodifieddate": _as_iso(raw.get("LastModifiedDate")),
        "createdate": _as_iso(raw.get("CreatedDate")),
        # DealSyncService expects HubSpot-style keys for probability/forecasting.
        "hs_deal_stage_probability": raw.get("Probability"),
        "hs_forecast_probability": raw.get("Probability"),
        "description": raw.get("Description"),
        "lead_source": raw.get("LeadSource"),
        "hs_forecast_category": raw.get("ForecastCategory"),
        # DealSyncService uses "dealtype".
        "dealtype": raw.get("Type"),
        # Keep provider-specific aliases for debugging/compatibility.
        "forecast_category": raw.get("ForecastCategory"),
        "probability": raw.get("Probability"),
        "type": raw.get("Type"),
        "hs_is_closed": is_closed,
        "hs_is_closed_won": is_won,
        "hs_is_closed_lost": is_closed_lost,
        # HubSpot-style close date fields (best-effort mapping).
        "hs_closed_won_date": raw.get("CloseDate") if is_won is True else None,
        "hs_closed_lost_date": raw.get("CloseDate") if is_closed_lost is True else None,
        # Owner details from relationship query (no separate User sync needed)
        "hubspot_owner_name": owner_name,
        "hubspot_owner_email": owner_email,
        "owner_first_name": owner_first_name,
        "owner_last_name": owner_last_name,
    }

    return {
        "id": raw.get("Id"),
        "createdAt": _as_iso(raw.get("CreatedDate")),
        "updatedAt": _as_iso(raw.get("LastModifiedDate")),
        "archived": bool(raw.get("IsDeleted")),
        "url": (raw.get("attributes") or {}).get("url"),
        "properties": {k: v for k, v in props.items() if v is not None},
        "associations": {},
        "data_payload": raw,
    }


def _sf_to_canonical_contact(raw: dict[str, Any]) -> dict[str, Any]:
    props: dict[str, Any] = {
        "email": raw.get("Email"),
        "firstname": raw.get("FirstName"),
        "lastname": raw.get("LastName"),
        "fullname": raw.get("Name"),
        "phone": raw.get("Phone"),
        "mobilephone": raw.get("MobilePhone"),
        "title": raw.get("Title"),
        "department": raw.get("Department"),
        "address": raw.get("MailingStreet"),
        "city": raw.get("MailingCity"),
        "state": raw.get("MailingState"),
        "zip": raw.get("MailingPostalCode"),
        "country": raw.get("MailingCountry"),
        "accountid": raw.get("AccountId"),
        "hubspot_owner_id": raw.get("OwnerId"),
        "lead_source": raw.get("LeadSource"),
        "hs_lastmodifieddate": _as_iso(raw.get("LastModifiedDate")),
        "createdate": _as_iso(raw.get("CreatedDate")),
    }

    return {
        "id": raw.get("Id"),
        "createdAt": _as_iso(raw.get("CreatedDate")),
        "updatedAt": _as_iso(raw.get("LastModifiedDate")),
        "archived": bool(raw.get("IsDeleted")),
        "properties": {k: v for k, v in props.items() if v is not None},
        "associations": {},
        "data_payload": raw,
    }


def _extract_domain_from_url(url: str | None) -> str | None:
    """Extract domain from a website URL."""
    if not url:
        return None
    try:
        from urllib.parse import urlparse

        # Add scheme if missing for proper parsing
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path.split("/")[0]
        # Remove www. prefix if present
        if domain.startswith("www."):
            domain = domain[4:]
        return domain if domain else None
    except Exception:
        return None


def _sf_to_canonical_company(raw: dict[str, Any]) -> dict[str, Any]:
    # Extract owner details from relationship query (Owner.Name, Owner.Email, etc.)
    owner_data = raw.get("Owner") or {}
    owner_name = owner_data.get("Name") if isinstance(owner_data, dict) else None
    owner_email = owner_data.get("Email") if isinstance(owner_data, dict) else None

    website = raw.get("Website")
    domain = _extract_domain_from_url(website)

    props: dict[str, Any] = {
        "name": raw.get("Name"),
        "domain": domain,  # Extracted from Website URL
        "website": website,
        "phone": raw.get("Phone"),
        "industry": raw.get("Industry"),
        "numberofemployees": raw.get("NumberOfEmployees"),
        "description": raw.get("Description"),
        "type": raw.get("Type"),  # Account Type → company_type
        "hubspot_owner_id": raw.get("OwnerId"),
        "hubspot_owner_name": owner_name,  # From Owner relationship
        "hubspot_owner_email": owner_email,  # From Owner relationship
        "address": raw.get("BillingStreet"),
        "city": raw.get("BillingCity"),
        "state": raw.get("BillingState"),
        "zip": raw.get("BillingPostalCode"),
        "country": raw.get("BillingCountry"),
        "hs_lastmodifieddate": _as_iso(raw.get("LastModifiedDate")),
        "createdate": _as_iso(raw.get("CreatedDate")),
    }

    return {
        "id": raw.get("Id"),
        "createdAt": _as_iso(raw.get("CreatedDate")),
        "updatedAt": _as_iso(raw.get("LastModifiedDate")),
        "archived": bool(raw.get("IsDeleted")),
        "properties": {k: v for k, v in props.items() if v is not None},
        "associations": {},
        "data_payload": raw,
    }


def _sf_to_canonical_user(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": raw.get("Id"),
        "email": raw.get("Email"),
        "firstName": raw.get("FirstName"),
        "lastName": raw.get("LastName"),
        "fullName": raw.get("Name"),
        "archived": not bool(raw.get("IsActive", True)),
        "properties": {k: v for k, v in raw.items() if v is not None},
        "data_payload": raw,
    }


def _sf_to_canonical_task(raw: dict[str, Any]) -> dict[str, Any]:
    # Extract owner details from relationship query (Owner.Name, Owner.Email, etc.)
    owner_data = raw.get("Owner") or {}
    owner_name = owner_data.get("Name") if isinstance(owner_data, dict) else None
    owner_email = owner_data.get("Email") if isinstance(owner_data, dict) else None

    props: dict[str, Any] = {
        "subject": raw.get("Subject"),
        "description": raw.get("Description"),
        "status": raw.get("Status"),
        "priority": raw.get("Priority"),
        "type": raw.get("Type"),
        "activity_date": raw.get("ActivityDate"),
        "owner_id": raw.get("OwnerId"),
        "who_id": raw.get("WhoId"),
        "what_id": raw.get("WhatId"),
        "created_date": _as_iso(raw.get("CreatedDate")),
        "last_modified_date": _as_iso(raw.get("LastModifiedDate")),
        "is_deleted": bool(raw.get("IsDeleted")),
        # Owner details from relationship query (no separate User sync needed)
        "hubspot_owner_id": raw.get("OwnerId"),  # Canonical key for owner ID
        "hubspot_owner_name": owner_name,
        "hubspot_owner_email": owner_email,
    }

    return {
        "id": raw.get("Id"),
        "createdAt": _as_iso(raw.get("CreatedDate")),
        "updatedAt": _as_iso(raw.get("LastModifiedDate")),
        "archived": bool(raw.get("IsDeleted")),
        "properties": {k: v for k, v in props.items() if v is not None},
        "associations": {},
        "data_payload": raw,
    }


def _sf_to_canonical_lead(raw: dict[str, Any]) -> dict[str, Any]:
    props: dict[str, Any] = {
        "email": raw.get("Email"),
        "firstname": raw.get("FirstName"),
        "lastname": raw.get("LastName"),
        "fullname": raw.get("Name"),
        "phone": raw.get("Phone"),
        "mobilephone": raw.get("MobilePhone"),
        "title": raw.get("Title"),
        "company": raw.get("Company"),
        "lifecyclestage": "lead",
        "leadstatus": raw.get("Status"),
        "lead_source": raw.get("LeadSource"),
        "hubspot_owner_id": raw.get("OwnerId"),
        "hs_lastmodifieddate": _as_iso(raw.get("LastModifiedDate")),
        "createdate": _as_iso(raw.get("CreatedDate")),
    }

    return {
        "id": raw.get("Id"),
        "createdAt": _as_iso(raw.get("CreatedDate")),
        "updatedAt": _as_iso(raw.get("LastModifiedDate")),
        "archived": bool(raw.get("IsDeleted")),
        "properties": {k: v for k, v in props.items() if v is not None},
        "associations": {},
        "data_payload": raw,
    }


def _sf_to_canonical_ticket(raw: dict[str, Any]) -> dict[str, Any]:
    """Transform Salesforce Case to canonical ticket format.

    Salesforce Cases are the equivalent of HubSpot Tickets - customer support
    requests, issues, or service tickets.
    """
    # Extract owner details from relationship query (Owner.Name, Owner.Email, etc.)
    owner_data = raw.get("Owner") or {}
    owner_name = owner_data.get("Name") if isinstance(owner_data, dict) else None
    owner_email = owner_data.get("Email") if isinstance(owner_data, dict) else None

    props: dict[str, Any] = {
        # Standard ticket fields (HubSpot-compatible)
        "subject": raw.get("Subject"),
        "content": raw.get("Description"),
        "hs_ticket_priority": raw.get("Priority"),
        "hs_pipeline_stage": raw.get("Status"),
        "hubspot_owner_id": raw.get("OwnerId"),
        "hs_lastmodifieddate": _as_iso(raw.get("LastModifiedDate")),
        "createdate": _as_iso(raw.get("CreatedDate")),
        "closed_date": _as_iso(raw.get("ClosedDate")),
        # Salesforce-specific fields
        "case_number": raw.get("CaseNumber"),
        "case_origin": raw.get("Origin"),
        "case_type": raw.get("Type"),
        "case_reason": raw.get("Reason"),
        "is_closed": raw.get("IsClosed"),
        "is_escalated": raw.get("IsEscalated"),
        # Related records
        "contact_id": raw.get("ContactId"),
        "account_id": raw.get("AccountId"),
        "parent_id": raw.get("ParentId"),
        # Web-to-case fields
        "supplied_name": raw.get("SuppliedName"),
        "supplied_email": raw.get("SuppliedEmail"),
        "supplied_phone": raw.get("SuppliedPhone"),
        "supplied_company": raw.get("SuppliedCompany"),
        # Owner details from relationship query (no separate User sync needed)
        "hubspot_owner_name": owner_name,
        "hubspot_owner_email": owner_email,
    }

    # Build associations from related records
    associations: dict[str, Any] = {}
    if raw.get("ContactId"):
        associations["contacts"] = {
            "results": [{"id": raw.get("ContactId"), "type": "ticket_to_contact"}]
        }
    if raw.get("AccountId"):
        associations["companies"] = {
            "results": [{"id": raw.get("AccountId"), "type": "ticket_to_company"}]
        }

    return {
        "id": raw.get("Id"),
        "createdAt": _as_iso(raw.get("CreatedDate")),
        "updatedAt": _as_iso(raw.get("LastModifiedDate")),
        "archived": bool(raw.get("IsDeleted")),
        "properties": {k: v for k, v in props.items() if v is not None},
        "associations": associations,
        "data_payload": raw,
    }


def _sf_to_canonical_deal_with_associations(
    raw: dict[str, Any],
    contact_roles_by_opp: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Transform Salesforce Opportunity with contact role associations.

    Embeds OpportunityContactRoles in the associations field to match
    HubSpot's format where contacts are nested in associations.contacts.results.
    """
    canonical = _sf_to_canonical_deal(raw)
    opp_id = raw.get("Id")

    # Build associations.contacts.results from OpportunityContactRoles
    roles = contact_roles_by_opp.get(opp_id, [])
    if roles:
        contacts_results = [
            {
                "id": role.get("ContactId"),
                "type": role.get("Role") or "contact",
                "isPrimary": bool(role.get("IsPrimary")),
                "roleId": role.get("Id"),
            }
            for role in roles
            if role.get("ContactId")
        ]
        canonical["associations"] = {
            "contacts": {"results": contacts_results},
        }

    # Also embed Account as a company association
    account_id = raw.get("AccountId")
    if account_id:
        companies_results = [{"id": account_id, "type": "company"}]
        if "contacts" in canonical["associations"]:
            canonical["associations"]["companies"] = {"results": companies_results}
        else:
            canonical["associations"] = {"companies": {"results": companies_results}}

    return canonical


async def fetch_opportunities(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,
    data_type: str | None,
    provider_config_key: str,
    *,
    sync_type: str | None = None,
    response_results: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Fetch Opportunities with associated contact roles.

    Fetches both Opportunities and OpportunityContactRoles, then embeds
    the contact roles into each deal's associations field for compatibility
    with the existing DealSyncService participant extraction.

    Args:
        nango: Nango client instance
        connection_id: Nango connection ID
        is_initial: Whether this is an initial (full) sync
        last_synced_at: Timestamp of last sync
        data_type: Data type being synced (unused, kept for interface)
        provider_config_key: Nango provider config key
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)
        response_results: Record counts from Nango webhook {added, updated, deleted}

    Returns:
        List of deal records with _sync_metadata added for incremental processing
    """
    _ = data_type

    # Fetch opportunities
    opp_fetcher = SalesforceOpportunityFetcher(nango, provider_config_key, connection_id)
    opportunities = await opp_fetcher.fetch_all(
        incremental=not is_initial,
        last_synced_at=last_synced_at,
    )

    # Fetch contact roles (always full sync to ensure we have all associations)
    role_fetcher = SalesforceContactRoleFetcher(nango, provider_config_key, connection_id)
    contact_roles = await role_fetcher.fetch_all(
        incremental=False,
        last_synced_at=None,
    )

    # Group contact roles by OpportunityId
    roles_by_opp: dict[str, list[dict[str, Any]]] = {}
    for role in contact_roles:
        opp_id = role.get("OpportunityId")
        if opp_id:
            roles_by_opp.setdefault(opp_id, []).append(role)

    # Transform opportunities with embedded associations
    canonical_records = [
        _sf_to_canonical_deal_with_associations(r, roles_by_opp) for r in opportunities
    ]

    # Add sync metadata for incremental processing optimization
    context = SyncContext.from_params(
        sync_type=sync_type,
        is_initial=is_initial,
        response_results=response_results,
    )

    # Classify records and add _sync_metadata
    # Note: For Salesforce, IsDeleted=True records will be classified as DELETED
    processed_records, counts = classify_records(
        canonical_records,
        context=context,
        add_metadata=True,
    )

    # Log action distribution for observability
    log_sync_action_distribution(
        counts,
        entity_type="deals",
        provider="salesforce",
        sync_type=sync_type,
    )

    logger.info(
        "Salesforce opportunities fetch completed: total=%d (webhook_expected: added=%d updated=%d deleted=%d)",
        len(processed_records),
        response_results.get("added", 0) if response_results else 0,
        response_results.get("updated", 0) if response_results else 0,
        response_results.get("deleted", 0) if response_results else 0,
    )

    return processed_records


async def fetch_deals(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,
    data_type: str | None,
    provider_config_key: str,
    *,
    sync_type: str | None = None,
    response_results: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Fetch deals (Opportunities) from Salesforce.

    Args:
        nango: Nango client instance
        connection_id: Nango connection ID
        is_initial: Whether this is an initial (full) sync
        last_synced_at: Timestamp of last sync
        data_type: Data type being synced (unused, kept for interface)
        provider_config_key: Nango provider config key
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)
        response_results: Record counts from Nango webhook {added, updated, deleted}

    Returns:
        List of deal records with _sync_metadata added for incremental processing
    """
    return await fetch_opportunities(
        nango=nango,
        connection_id=connection_id,
        is_initial=is_initial,
        last_synced_at=last_synced_at,
        data_type=data_type,
        provider_config_key=provider_config_key,
        sync_type=sync_type,
        response_results=response_results,
    )


async def fetch_contacts(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,
    data_type: str | None,
    provider_config_key: str,
    *,
    sync_type: str | None = None,
    response_results: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Fetch contacts from Salesforce.

    Args:
        nango: Nango client instance
        connection_id: Nango connection ID
        is_initial: Whether this is an initial (full) sync
        last_synced_at: Timestamp of last sync
        data_type: Data type being synced (unused, kept for interface)
        provider_config_key: Nango provider config key
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)
        response_results: Record counts from Nango webhook {added, updated, deleted}

    Returns:
        List of contact records with _sync_metadata added for incremental processing
    """
    _ = data_type
    fetcher = SalesforceContactFetcher(nango, provider_config_key, connection_id)
    rows = await fetcher.fetch_all(
        incremental=not is_initial,
        last_synced_at=last_synced_at,
    )
    canonical_records = [_sf_to_canonical_contact(r) for r in rows]

    # Add sync metadata for incremental processing optimization
    context = SyncContext.from_params(
        sync_type=sync_type,
        is_initial=is_initial,
        response_results=response_results,
    )

    processed_records, counts = classify_records(
        canonical_records,
        context=context,
        add_metadata=True,
    )

    log_sync_action_distribution(
        counts,
        entity_type="contacts",
        provider="salesforce",
        sync_type=sync_type,
    )

    logger.info(
        "Salesforce contacts fetch completed: total=%d (webhook_expected: added=%d updated=%d deleted=%d)",
        len(processed_records),
        response_results.get("added", 0) if response_results else 0,
        response_results.get("updated", 0) if response_results else 0,
        response_results.get("deleted", 0) if response_results else 0,
    )

    return processed_records


async def fetch_leads(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,
    data_type: str | None,
    provider_config_key: str,
) -> list[dict[str, Any]]:
    _ = data_type
    fetcher = SalesforceLeadFetcher(nango, provider_config_key, connection_id)
    rows = await fetcher.fetch_all(
        incremental=not is_initial,
        last_synced_at=last_synced_at,
    )
    return [_sf_to_canonical_lead(r) for r in rows]


async def fetch_accounts(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,
    data_type: str | None,
    provider_config_key: str,
    *,
    sync_type: str | None = None,
    response_results: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Fetch accounts (companies) from Salesforce.

    Args:
        nango: Nango client instance
        connection_id: Nango connection ID
        is_initial: Whether this is an initial (full) sync
        last_synced_at: Timestamp of last sync
        data_type: Data type being synced (unused, kept for interface)
        provider_config_key: Nango provider config key
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)
        response_results: Record counts from Nango webhook {added, updated, deleted}

    Returns:
        List of company records with _sync_metadata added for incremental processing
    """
    _ = data_type
    fetcher = SalesforceAccountFetcher(nango, provider_config_key, connection_id)
    rows = await fetcher.fetch_all(
        incremental=not is_initial,
        last_synced_at=last_synced_at,
    )
    canonical_records = [_sf_to_canonical_company(r) for r in rows]

    # Add sync metadata for incremental processing optimization
    context = SyncContext.from_params(
        sync_type=sync_type,
        is_initial=is_initial,
        response_results=response_results,
    )

    processed_records, counts = classify_records(
        canonical_records,
        context=context,
        add_metadata=True,
    )

    log_sync_action_distribution(
        counts,
        entity_type="companies",
        provider="salesforce",
        sync_type=sync_type,
    )

    logger.info(
        "Salesforce accounts fetch completed: total=%d (webhook_expected: added=%d updated=%d deleted=%d)",
        len(processed_records),
        response_results.get("added", 0) if response_results else 0,
        response_results.get("updated", 0) if response_results else 0,
        response_results.get("deleted", 0) if response_results else 0,
    )

    return processed_records


async def fetch_companies(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,
    data_type: str | None,
    provider_config_key: str,
    *,
    sync_type: str | None = None,
    response_results: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Fetch companies (accounts) from Salesforce - alias for fetch_accounts."""
    return await fetch_accounts(
        nango=nango,
        connection_id=connection_id,
        is_initial=is_initial,
        last_synced_at=last_synced_at,
        data_type=data_type,
        provider_config_key=provider_config_key,
        sync_type=sync_type,
        response_results=response_results,
    )


async def fetch_users(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,
    data_type: str | None,
    provider_config_key: str,
    *,
    sync_type: str | None = None,
    response_results: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Fetch users from Salesforce.

    Args:
        nango: NangoClient instance
        connection_id: Nango connection ID
        is_initial: Whether this is initial sync
        last_synced_at: Last sync timestamp
        data_type: Data type being synced
        provider_config_key: Provider config key
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)
        response_results: Record counts from Nango webhook for classification
    """
    _ = data_type
    fetcher = SalesforceUserFetcher(nango, provider_config_key, connection_id)
    rows = await fetcher.fetch_all(
        incremental=not is_initial,
        last_synced_at=last_synced_at,
    )
    users = [_sf_to_canonical_user(r) for r in rows]

    # Add sync metadata for incremental sync
    if sync_type and sync_type.upper() == "INCREMENTAL" and users:
        context = SyncContext(response_results=response_results)
        classify_records(users, context)
        log_sync_action_distribution(users, "users", logger)

    return users


async def fetch_tasks(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,
    data_type: str | None,
    provider_config_key: str,
    *,
    sync_type: str | None = None,
    response_results: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Fetch tasks from Salesforce.

    Args:
        nango: NangoClient instance
        connection_id: Nango connection ID
        is_initial: Whether this is initial sync
        last_synced_at: Last sync timestamp
        data_type: Data type being synced
        provider_config_key: Provider config key
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)
        response_results: Record counts from Nango webhook for classification
    """
    _ = data_type
    fetcher = SalesforceTaskFetcher(nango, provider_config_key, connection_id)
    rows = await fetcher.fetch_all(
        incremental=not is_initial,
        last_synced_at=last_synced_at,
    )
    tasks = [_sf_to_canonical_task(r) for r in rows]

    # Add sync metadata for incremental sync
    if sync_type and sync_type.upper() == "INCREMENTAL" and tasks:
        context = SyncContext(response_results=response_results)
        classify_records(tasks, context)
        log_sync_action_distribution(tasks, "tasks", logger)

    return tasks


async def fetch_notes(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,
    data_type: str | None,
    provider_config_key: str,
    *,
    sync_type: str | None = None,
    response_results: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Fetch legacy Salesforce Notes.

    Notes attached to Opportunities will be linked to deals during sync.

    Args:
        nango: NangoClient instance
        connection_id: Nango connection ID
        is_initial: Whether this is initial sync
        last_synced_at: Last sync timestamp
        data_type: Data type being synced
        provider_config_key: Provider config key
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)
        response_results: Record counts from Nango webhook for classification
    """
    _ = data_type
    fetcher = SalesforceNoteFetcher(nango, provider_config_key, connection_id)
    rows = await fetcher.fetch_all(
        incremental=not is_initial,
        last_synced_at=last_synced_at,
    )
    notes = [_sf_to_canonical_note(r) for r in rows]

    # Add sync metadata for incremental sync
    if sync_type and sync_type.upper() == "INCREMENTAL" and notes:
        context = SyncContext(response_results=response_results)
        classify_records(notes, context)
        log_sync_action_distribution(notes, "notes", logger)

    return notes


async def fetch_tickets(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,
    data_type: str | None,
    provider_config_key: str,
    *,
    sync_type: str | None = None,
    response_results: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Fetch tickets (Cases) from Salesforce.

    In Salesforce, tickets are represented by the Case object, which is the
    equivalent of HubSpot Tickets - customer support requests and issues.

    Args:
        nango: NangoClient instance
        connection_id: Nango connection ID
        is_initial: Whether this is initial sync
        last_synced_at: Last sync timestamp
        data_type: Data type being synced
        provider_config_key: Provider config key
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)
        response_results: Record counts from Nango webhook for classification
    """
    _ = data_type
    fetcher = SalesforceCaseFetcher(nango, provider_config_key, connection_id)
    rows = await fetcher.fetch_all(
        incremental=not is_initial,
        last_synced_at=last_synced_at,
    )
    tickets = [_sf_to_canonical_ticket(r) for r in rows]

    # Add sync metadata for incremental sync
    if sync_type and sync_type.upper() == "INCREMENTAL" and tickets:
        context = SyncContext(response_results=response_results)
        classify_records(tickets, context)
        log_sync_action_distribution(tickets, "tickets", logger)

    return tickets


async def fetch_pipelines(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,
    data_type: str | None,
    provider_config_key: str,
    *,
    sync_type: str | None = None,
    response_results: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Fetch pipelines from Salesforce.

    Args:
        nango: NangoClient instance
        connection_id: Nango connection ID
        is_initial: Whether this is initial sync
        last_synced_at: Last sync timestamp
        data_type: Data type being synced
        provider_config_key: Provider config key
        sync_type: Sync type from Nango webhook (INITIAL or INCREMENTAL)
        response_results: Record counts from Nango webhook for classification
    """
    _ = data_type, is_initial, last_synced_at
    fetcher = SalesforcePipelineFetcher(nango, provider_config_key, connection_id)
    stages = await fetcher.fetch_all(
        incremental=False,
        last_synced_at=None,
    )

    # Represent Salesforce opportunity stages as a single pipeline.
    pipeline = {
        "id": "salesforce_opportunity",
        "label": "Salesforce Opportunity",
        "objectType": "deals",
        "default": True,
        "archived": False,
        "displayOrder": 0,
        "stages": [
            {
                # Match Opportunity.StageName (label) for dealstage joins.
                "id": s.get("MasterLabel") or s.get("ApiName") or s.get("Id"),
                "stageId": s.get("MasterLabel") or s.get("ApiName") or s.get("Id"),
                "label": s.get("MasterLabel") or s.get("ApiName"),
                "displayOrder": s.get("SortOrder"),
                "metadata": {
                    "isClosed": s.get("IsClosed"),
                    "probability": s.get("DefaultProbability"),
                    "isWon": s.get("IsWon"),
                },
                "updatedAt": None,
            }
            for s in stages
            if isinstance(s, dict)
        ],
    }

    pipelines = [pipeline]

    # Add sync metadata for incremental sync
    if sync_type and sync_type.upper() == "INCREMENTAL" and pipelines:
        context = SyncContext(response_results=response_results)
        classify_records(pipelines, context)
        log_sync_action_distribution(pipelines, "pipelines", logger)

    return pipelines


def _sf_contact_role_to_participant(raw: dict[str, Any]) -> dict[str, Any]:
    """Transform Salesforce OpportunityContactRole to a participant payload.

    Returns a participant structure compatible with DealSyncService._upsert_participants().
    """
    return {
        "external_id": raw.get("Id"),
        "deal_external_id": raw.get("OpportunityId"),
        "related_external_id": raw.get("ContactId"),
        "relationship_role": raw.get("Role"),
        "is_primary": bool(raw.get("IsPrimary")),
        "participant_type": "contact",  # Will be converted to CRMParticipantType.CONTACT
        "source_modified_at": raw.get("LastModifiedDate"),
        "raw": raw,
    }


async def fetch_contact_roles(
    nango: NangoClient,
    connection_id: str,
    is_initial: bool,
    last_synced_at: Any,
    data_type: str | None,
    provider_config_key: str,
) -> list[dict[str, Any]]:
    """Fetch OpportunityContactRole associations.

    These represent Contact ↔ Opportunity relationships with roles like
    "Decision Maker", "Economic Buyer", etc. They are synced as CRMDealParticipant
    records with participant_type=CONTACT.
    """
    _ = data_type
    fetcher = SalesforceContactRoleFetcher(nango, provider_config_key, connection_id)
    rows = await fetcher.fetch_all(
        incremental=not is_initial,
        last_synced_at=last_synced_at,
    )
    return [_sf_contact_role_to_participant(r) for r in rows]
