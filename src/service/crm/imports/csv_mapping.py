from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from src.service.crm.utils.account import (
    is_account_linkedin_url,
    is_person_linkedin_url,
    normalize_account_domain,
)
from src.service.crm.utils.identity import normalize_email

MappingConfidence = Literal["high", "medium", "low", "unmapped"]

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

_HEADER_ALIASES: dict[str, str] = {
    "company": "account.name",
    "company name": "account.name",
    "account": "account.name",
    "account name": "account.name",
    "website": "account.website",
    "company website": "account.website",
    "account website": "account.website",
    "domain": "account.domain",
    "company domain": "account.domain",
    "account domain": "account.domain",
    "account phone": "account.phone",
    "company phone": "account.phone",
    "account city": "account.city",
    "company city": "account.city",
    "account state": "account.state",
    "company state": "account.state",
    "account country": "account.country",
    "company country": "account.country",
    "account address": "account.address",
    "company address": "account.address",
    "account postal code": "account.postal_code",
    "account zip": "account.postal_code",
    "company postal code": "account.postal_code",
    "company zip": "account.postal_code",
    "first name": "contact.first_name",
    "firstname": "contact.first_name",
    "last name": "contact.last_name",
    "lastname": "contact.last_name",
    "full name": "contact.name",
    "name": "contact.name",
    "contact name": "contact.name",
    "email": "contact.email",
    "email address": "contact.email",
    "contact email": "contact.email",
    "title": "contact.job_title",
    "job title": "contact.job_title",
    "linkedin": "contact.linkedin_url",
    "linkedin url": "contact.linkedin_url",
    "contact linkedin": "contact.linkedin_url",
    "contact linkedin url": "contact.linkedin_url",
    "phone": "contact.phone",
    "phone number": "contact.phone",
    "mobile phone": "contact.mobile_phone",
    "mobile": "contact.mobile_phone",
    "contact website": "contact.website",
    "contact address": "contact.address",
    "contact city": "contact.city",
    "contact state": "contact.state",
    "contact country": "contact.country",
    "contact postal code": "contact.postal_code",
    "contact zip": "contact.postal_code",
    "company linkedin": "account.linkedin_url",
    "company linkedin url": "account.linkedin_url",
    "account linkedin": "account.linkedin_url",
    "account linkedin url": "account.linkedin_url",
    "deal name": "deal.name",
    "opportunity name": "deal.name",
    "amount": "deal.amount",
    "deal amount": "deal.amount",
    "deal value": "deal.amount",
    "currency": "deal.currency",
    "created date": "deal.source_created_at",
    "created at": "deal.source_created_at",
    "source created at": "deal.source_created_at",
    "expected close date": "deal.expected_close_date",
    "close date": "deal.expected_close_date",
    "deal type": "deal.deal_type",
    "priority": "deal.priority",
    "lead source": "deal.lead_source",
    "probability": "deal.probability",
    "forecast category": "deal.forecast_category",
    "description": "deal.description",
    "deal description": "deal.description",
}


@dataclass(frozen=True)
class CSVMappingSuggestion:
    header: str
    target_field: str | None
    confidence: MappingConfidence
    reason_code: str


def suggest_csv_mappings(
    headers: list[str],
    sample_rows: list[dict[str, str]],
) -> list[CSVMappingSuggestion]:
    return [_suggest_header_mapping(header, sample_rows) for header in headers]


def _suggest_header_mapping(
    header: str,
    sample_rows: list[dict[str, str]],
) -> CSVMappingSuggestion:
    alias_target = _HEADER_ALIASES.get(_normalize_header(header))
    sample_target, sample_reason = _sample_mapping(header, sample_rows)

    if alias_target and sample_target == alias_target:
        return CSVMappingSuggestion(header, alias_target, "high", "alias_and_sample")
    if alias_target:
        return CSVMappingSuggestion(header, alias_target, "medium", "header_alias")
    if sample_target and sample_reason:
        return CSVMappingSuggestion(header, sample_target, "medium", sample_reason)
    return CSVMappingSuggestion(header, None, "unmapped", "no_match")


def _sample_mapping(
    header: str,
    sample_rows: list[dict[str, str]],
) -> tuple[str | None, str | None]:
    values = [
        value.strip()
        for row in sample_rows
        if isinstance(row.get(header), str) and (value := row[header].strip())
    ]
    if not values:
        return None, None
    if _mostly(values, _is_email):
        return "contact.email", "sample_email"
    if _mostly(values, is_account_linkedin_url):
        return "account.linkedin_url", "sample_account_linkedin"
    if _mostly(values, is_person_linkedin_url):
        return "contact.linkedin_url", "sample_person_linkedin"
    if _mostly(values, _is_website):
        return "account.website", "sample_website"
    return None, None


def _normalize_header(header: str) -> str:
    return " ".join(_NON_ALNUM_RE.sub(" ", header.strip().lower()).split())


def _mostly(values: list[str], predicate) -> bool:
    matches = sum(1 for value in values if predicate(value))
    return matches > 0 and matches / len(values) >= 0.8


def _is_email(value: str) -> bool:
    normalized = normalize_email(value)
    return bool(normalized and "@" in normalized and "." in normalized.rsplit("@", 1)[-1])


def _is_website(value: str) -> bool:
    if is_account_linkedin_url(value) or is_person_linkedin_url(value):
        return False
    domain = normalize_account_domain(value)
    return bool(domain and "." in domain)
