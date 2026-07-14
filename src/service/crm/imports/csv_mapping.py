"""Deterministic CSV header and sample-value mapping suggestions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from src.service.crm.utils.account import (
    is_account_linkedin_url,
    is_person_linkedin_url,
    normalize_account_domain,
)
from src.service.crm.utils.identity import is_valid_email

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
    "owner": "deal.owner",
    "deal owner": "deal.owner",
    "salesperson": "deal.owner",
    "stage": "deal.stage",
    "deal stage": "deal.stage",
}


@dataclass(frozen=True)
class CSVMappingSuggestion:
    """One explainable mapping suggestion for an uploaded CSV header."""

    header: str
    target_field: str | None
    confidence: MappingConfidence
    reason_code: str


def suggest_csv_mappings(
    headers: list[str],
    sample_rows: list[dict[str, str]],
    *,
    targets: list[dict] | None = None,
    stage_values: set[str] | None = None,
) -> list[CSVMappingSuggestion]:
    """Suggest unique targets from deterministic header and representative-value evidence."""
    label_candidates: dict[str, set[str]] = {}
    for item in targets or []:
        if not item.get("target_field"):
            continue
        for candidate in (item.get("label"), item.get("custom_field_key")):
            normalized = _normalize_header(str(candidate or ""))
            if normalized:
                label_candidates.setdefault(normalized, set()).add(str(item["target_field"]))
    label_targets = {
        label: next(iter(matches))
        for label, matches in label_candidates.items()
        if len(matches) == 1
    }
    target_types = {
        str(item["target_field"]): str(item.get("value_type") or "text")
        for item in targets or []
        if item.get("target_field")
    }
    suggestions = [
        _suggest_header_mapping(
            header,
            sample_rows,
            label_targets=label_targets,
            target_types=target_types,
            stage_values=stage_values or set(),
        )
        for header in headers
    ]
    return _enforce_unique_targets(suggestions)


def _suggest_header_mapping(
    header: str,
    sample_rows: list[dict[str, str]],
    *,
    label_targets: dict[str, str],
    target_types: dict[str, str],
    stage_values: set[str],
) -> CSVMappingSuggestion:
    normalized_header = _normalize_header(header)
    alias_target = _HEADER_ALIASES.get(normalized_header) or label_targets.get(normalized_header)
    sample_target, sample_reason = _sample_mapping(
        header,
        sample_rows,
        alias_target=alias_target,
        target_types=target_types,
        stage_values=stage_values,
    )

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
    *,
    alias_target: str | None,
    target_types: dict[str, str],
    stage_values: set[str],
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
    if stage_values and _mostly(values, lambda value: _normalize_header(value) in stage_values):
        return "deal.stage", "sample_pipeline_stage"
    if alias_target:
        value_type = target_types.get(alias_target)
        detectors = {
            "currency": _is_currency,
            "number": _is_number,
            "integer": _is_integer,
            "percentage": _is_percentage,
            "boolean": _is_boolean,
            "date": _is_date,
            "datetime": _is_date,
            "owner": lambda value: bool(value.strip()),
            "stage": lambda value: _normalize_header(value) in stage_values,
            "text": lambda value: bool(value.strip()),
        }
        detector = detectors.get(value_type or "")
        if detector and _mostly(values, detector):
            return alias_target, "header_and_sample"
    return None, None


def _enforce_unique_targets(
    suggestions: list[CSVMappingSuggestion],
) -> list[CSVMappingSuggestion]:
    """Prevent automatic mappings from assigning one CRM field more than once."""
    by_target: dict[str, list[int]] = {}
    for index, suggestion in enumerate(suggestions):
        if suggestion.target_field:
            by_target.setdefault(suggestion.target_field, []).append(index)
    result = list(suggestions)
    rank = {"high": 3, "medium": 2, "low": 1, "unmapped": 0}
    for indexes in by_target.values():
        if len(indexes) == 1:
            continue
        best_rank = max(rank[result[index].confidence] for index in indexes)
        winners = [index for index in indexes if rank[result[index].confidence] == best_rank]
        keep = winners[0] if len(winners) == 1 else None
        for index in indexes:
            if index == keep:
                continue
            result[index] = CSVMappingSuggestion(
                result[index].header, None, "unmapped", "duplicate_or_ambiguous_target"
            )
    return result


def _normalize_header(header: str) -> str:
    return " ".join(_NON_ALNUM_RE.sub(" ", header.strip().lower()).split())


def _mostly(values: list[str], predicate) -> bool:
    matches = sum(1 for value in values if predicate(value))
    return matches > 0 and matches / len(values) >= 0.8


def _is_email(value: str) -> bool:
    return is_valid_email(value)


def _is_website(value: str) -> bool:
    if is_account_linkedin_url(value) or is_person_linkedin_url(value):
        return False
    domain = normalize_account_domain(value)
    return bool(domain and "." in domain)


def _is_number(value: str) -> bool:
    normalized = re.sub(r"[^0-9.,+\-]", "", value).replace(",", "")
    return bool(re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", normalized))


def _is_currency(value: str) -> bool:
    return _is_number(value) and bool(re.search(r"[$€£₹]|\b[A-Z]{3}\b|,", value))


def _is_integer(value: str) -> bool:
    return bool(re.fullmatch(r"[+-]?\d+", value.strip().replace(",", "")))


def _is_percentage(value: str) -> bool:
    text = value.strip().removesuffix("%")
    if not _is_number(text):
        return False
    parsed = float(text.replace(",", ""))
    return 0 <= parsed <= 100


def _is_boolean(value: str) -> bool:
    return value.strip().lower() in {"true", "false", "yes", "no", "1", "0"}


def _is_date(value: str) -> bool:
    text = value.strip().replace("Z", "+00:00")
    try:
        datetime.fromisoformat(text)
        return True
    except ValueError:
        pass
    for date_format in ("%d-%b-%Y", "%d %b %Y", "%b %d %Y", "%B %d %Y"):
        try:
            datetime.strptime(text, date_format)
            return True
        except ValueError:
            continue
    return bool(re.fullmatch(r"\d{1,2}[/-]\d{1,2}[/-](?:\d{2}|\d{4})", text))
