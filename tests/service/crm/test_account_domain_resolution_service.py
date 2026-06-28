from __future__ import annotations

import pytest
from src.service.crm.accounts.domain_resolution_service import AccountDomainResolutionService


@pytest.mark.parametrize(
    "email",
    [
        "jane@gmail.com",
        "jane@enter.gmail.com",
        "jane@outlook.com",
        "jane@mail.outlook.com",
        "jane@yahoo.com",
        "jane@abc.yahoo.com",
        "jane@icloud.com",
    ],
)
def test_derive_safe_domain_from_email_blocks_free_email_providers(email: str) -> None:
    assert AccountDomainResolutionService.derive_safe_domain_from_email(email) is None


@pytest.mark.parametrize(
    ("email", "domain"),
    [
        ("jane@acme.com", "acme.com"),
        ("jane@test.hackme.com", "test.hackme.com"),
        (" Jane@Sub.Acme.COM ", "sub.acme.com"),
    ],
)
def test_derive_safe_domain_from_email_keeps_business_subdomains(
    email: str,
    domain: str,
) -> None:
    assert AccountDomainResolutionService.derive_safe_domain_from_email(email) == domain


@pytest.mark.parametrize("email", ["not-an-email", "", None])
def test_derive_safe_domain_from_email_returns_none_for_invalid_email(email: str | None) -> None:
    assert AccountDomainResolutionService.derive_safe_domain_from_email(email) is None
