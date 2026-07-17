"""Account/contact identity string helpers for CRM write paths."""

from __future__ import annotations

import re
from urllib.parse import ParseResult, urlparse

_DOMAIN_PROTOCOL_RE = re.compile(r"^https?://", re.IGNORECASE)
_DOMAIN_WWW_RE = re.compile(r"^www\.", re.IGNORECASE)
_COMPANY_NAME_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_LINKEDIN_ACCOUNT_PATH_PREFIXES = ("/company/", "/school/", "/showcase/")
_LINKEDIN_PERSON_PATH_PREFIXES = ("/in/", "/pub/")
ACCOUNT_LINKEDIN_URL_ERROR = "Use a LinkedIn company, school, or showcase URL for accounts"
PERSON_LINKEDIN_URL_ERROR = "Use a LinkedIn profile URL for contacts and stakeholders"


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


def is_account_linkedin_url(value: str | None) -> bool:
    """Return whether a LinkedIn URL points to an organization-style page."""
    path = _linkedin_path(value)
    if path is None:
        return False
    return any(path.startswith(prefix) for prefix in _LINKEDIN_ACCOUNT_PATH_PREFIXES)


def is_person_linkedin_url(value: str | None) -> bool:
    """Return whether a LinkedIn URL points to a person profile page."""
    path = _linkedin_path(value)
    if path is None:
        return False
    return any(path.startswith(prefix) for prefix in _LINKEDIN_PERSON_PATH_PREFIXES)


def normalize_linkedin_url(value: str | None) -> str | None:
    """Return a scheme-insensitive LinkedIn host/path identity key."""
    parsed = _parse_candidate_url(value)
    if parsed is None:
        return None
    host = (parsed.netloc or "").lower().removeprefix("www.")
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    path = (parsed.path or "").lower().rstrip("/")
    return f"linkedin.com{path}" if path else None


def _linkedin_path(value: str | None) -> str | None:
    parsed = _parse_candidate_url(value)
    if parsed is None:
        return None
    host = (parsed.netloc or "").lower()
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return None
    return (parsed.path or "").lower()


def _parse_candidate_url(value: str | None) -> ParseResult | None:
    """Parse untrusted CRM text as a URL without leaking parser errors to callers."""
    cleaned = clean_optional_string(value)
    if not cleaned:
        return None
    try:
        return urlparse(cleaned if "://" in cleaned else f"https://{cleaned}")
    except ValueError:
        # Bracketed notes resemble IPv6 hosts to urllib; they are ordinary
        # non-URL input during CSV mapping and should simply fail detection.
        return None
