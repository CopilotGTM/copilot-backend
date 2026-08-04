import pytest
from pydantic import ValidationError
from src.domain.models.user_signup_models import ExternalSignupRequest


@pytest.mark.parametrize(
    "field",
    [
        "first_name",
        "last_name",
        "primary_domain",
        "country",
        "terms_version",
        "privacy_version",
        "website_url",
        "user_role",
        "status",
        "organisation_id",
        "email",
    ],
)
def test_signup_rejects_server_owned_fields(field: str) -> None:
    """Provider identity and authority fields must never be browser-authored."""
    with pytest.raises(ValidationError):
        ExternalSignupRequest.model_validate({"organisation_name": "Acme", field: "attacker-value"})


def test_signup_accepts_and_trims_organisation_name() -> None:
    """The verified founder may choose only the workspace display name."""
    request = ExternalSignupRequest.model_validate({"organisation_name": " Acme "})

    assert request.organisation_name == "Acme"


@pytest.mark.parametrize("organisation_name", ["", " ", "A"])
def test_signup_rejects_invalid_organisation_name(organisation_name: str) -> None:
    """A workspace name must contain at least two non-whitespace characters."""
    with pytest.raises(ValidationError):
        ExternalSignupRequest.model_validate({"organisation_name": organisation_name})
