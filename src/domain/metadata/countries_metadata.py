"""Country metadata.

For now this is a code-owned registry used by admin APIs (e.g. organisation provisioning).
In the future, this should be migrated to a master-table for auditability and runtime edits.

Notes:
- Country codes are ISO-3166-1 alpha-2.
- Keep this list intentionally small until the policy is finalized.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CountryMetadata:
    code: str
    name: str
    allowed_for_org_provisioning: bool = True


COUNTRIES: dict[str, CountryMetadata] = {
    "IN": CountryMetadata(code="IN", name="India", allowed_for_org_provisioning=True),
    "US": CountryMetadata(code="US", name="United States", allowed_for_org_provisioning=True),
}


def get_allowed_org_countries() -> list[CountryMetadata]:
    """Return the allowlisted countries for organisation provisioning."""
    allowed = [c for c in COUNTRIES.values() if c.allowed_for_org_provisioning]
    return sorted(allowed, key=lambda c: c.name)
