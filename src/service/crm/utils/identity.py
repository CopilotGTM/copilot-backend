"""Canonical email normalization shared by CRM identity boundaries."""

from __future__ import annotations

from pydantic import EmailStr, TypeAdapter, ValidationError

_EMAIL_ADAPTER = TypeAdapter(EmailStr)


def normalize_email(email: str | None) -> str | None:
    """Return the canonical CRM email key used for matching and duplicate checks."""
    if email is None:
        return None
    normalized = email.strip().lower()
    return normalized or None


def is_valid_email(email: str | None) -> bool:
    """Return whether a non-empty value is a syntactically valid email address."""
    normalized = normalize_email(email)
    if not normalized:
        return False
    try:
        _EMAIL_ADAPTER.validate_python(normalized)
    except ValidationError:
        return False
    return True


def normalize_spoken_email_query(query: str) -> str:
    """Normalize an email or a clearly spoken ``at``/``dot`` email phrase.

    General prose is left alone so a person's name containing either word is
    never rewritten as an address. Validation remains the caller's decision.
    """
    normalized = " ".join(query.strip().lower().split())
    if "@" in normalized:
        return normalized.replace(" ", "")
    if " at " not in normalized or " dot " not in normalized:
        return normalized
    local_part, domain = normalized.split(" at ", 1)
    candidate = f"{local_part.replace(' ', '')}@{domain.replace(' dot ', '.').replace(' ', '')}"
    return candidate if is_valid_email(candidate) else normalized


def extract_email_domain(email: str | None) -> str | None:
    """Return the lowercase domain segment from an email-like value."""
    normalized = normalize_email(email)
    if not normalized or "@" not in normalized:
        return None
    domain = normalized.rsplit("@", 1)[1].strip()
    return domain or None
