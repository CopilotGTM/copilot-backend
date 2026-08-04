import pytest
from pydantic import ValidationError
from src.domain.models.admin_org_models import (
    CreateOrganizationRequest,
    UpdateOrganizationRequest,
)


def test_organisation_domains_are_normalized_and_deduplicated() -> None:
    request = CreateOrganizationRequest(
        organisation_name="Acme",
        domain=[" ACME.COM. ", "acme.com"],
    )

    assert request.domain == ["acme.com"]


def test_organisation_domain_rejects_email_address() -> None:
    with pytest.raises(ValidationError):
        UpdateOrganizationRequest(domain=["person@acme.com"])
