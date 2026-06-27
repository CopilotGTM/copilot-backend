from __future__ import annotations

import re

_DOMAIN_PROTOCOL_RE = re.compile(r"^https?://", re.IGNORECASE)
_DOMAIN_WWW_RE = re.compile(r"^www\.", re.IGNORECASE)
_COMPANY_NAME_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def clean_optional_string(value: str | None) -> str | None:
    """Normalize optional user/provider text before matching or persistence."""
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def normalize_account_domain(domain: str | None) -> str | None:
    """Return the canonical domain key used for CRM account matching."""
    if not domain or not domain.strip():
        return None
    normalized = domain.strip()
    normalized = _DOMAIN_PROTOCOL_RE.sub("", normalized)
    normalized = _DOMAIN_WWW_RE.sub("", normalized)
    normalized = normalized.split("/")[0].split("?")[0].split("#")[0]
    return normalized.lower() or None


def normalize_company_name(value: str | None) -> str | None:
    """Return the normalized company-name key used for safe exact matching."""
    cleaned = clean_optional_string(value)
    if not cleaned:
        return None
    cleaned = cleaned.lower()
    cleaned = _COMPANY_NAME_NON_ALNUM_RE.sub(" ", cleaned)
    cleaned = " ".join(cleaned.split())
    return cleaned or None
