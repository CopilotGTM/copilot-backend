"""Safe account-domain inference from contact email addresses.

Contact creation uses email domains as sales account evidence only when the
domain looks like a business domain. Personal email providers are deliberately
blocked so automatic account creation does not pollute CRM data.
"""

from __future__ import annotations

import re

from src.service.crm.guards.internal_user_guard import FREE_EMAIL_DOMAINS
from src.service.crm.utils.identity import extract_email_domain

_DOMAIN_PROTOCOL_RE = re.compile(r"^https?://", re.IGNORECASE)
_DOMAIN_WWW_RE = re.compile(r"^www\.", re.IGNORECASE)


class AccountDomainResolutionService:
    """Policy service for safe business-domain extraction."""

    @classmethod
    def derive_safe_domain_from_email(cls, email: str | None) -> str | None:
        """Return a business domain eligible for automatic account resolution."""
        domain = cls.normalize_domain(extract_email_domain(email))
        if not domain or not cls.domain_is_safe_for_account_resolution(domain):
            return None
        return domain

    @staticmethod
    def normalize_domain(domain: str | None) -> str | None:
        """Return the canonical domain key used for account lookup and creation."""
        if not domain or not domain.strip():
            return None
        cleaned = domain.strip().lower()
        cleaned = _DOMAIN_PROTOCOL_RE.sub("", cleaned)
        cleaned = _DOMAIN_WWW_RE.sub("", cleaned)
        cleaned = cleaned.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        return cleaned or None

    @classmethod
    def domain_is_safe_for_account_resolution(cls, domain: str | None) -> bool:
        """Return whether a domain should drive automatic account inference."""
        normalized = cls.normalize_domain(domain)
        if not normalized:
            return False
        if not cls._domain_labels_are_valid(normalized):
            return False
        return not cls._is_free_email_provider_domain(normalized)

    @staticmethod
    def _is_free_email_provider_domain(domain: str) -> bool:
        return any(
            domain == provider_domain or domain.endswith(f".{provider_domain}")
            for provider_domain in FREE_EMAIL_DOMAINS
        )

    @staticmethod
    def _domain_labels_are_valid(domain: str) -> bool:
        labels = domain.split(".")
        return len(labels) >= 2 and all(
            label and not label.startswith("-") and not label.endswith("-") for label in labels
        )
