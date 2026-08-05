"""API contracts for user-owned CRM collection sorting."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from src.api.dependencies.master_dependencies import get_master_async_session
from src.api.dependencies.organisation_dependencies import (
    UserIDs,
    get_current_user,
    get_org_async_session,
)
from src.api.dependencies.rate_limit import rate_limit_user_read, rate_limit_user_write
from src.api.schemas.crm_schema_models import CRMEntitySchemaResponse, CRMFieldSchemaItem


@pytest.fixture(autouse=True)
def isolate_preference_routes_from_runtime_startup(mocker) -> None:
    """Route contracts do not need the live shared-tenant startup probes."""
    mocker.patch(
        "src.infra.database.tenant_runtime_config.validate_current_shared_tenant_runtime_startup"
    )
    mocker.patch(
        "src.infra.database.tenant_runtime_credentials."
        "validate_current_shared_tenant_runtime_password",
        new=AsyncMock(),
    )


class _FakeResult:
    def __init__(self, user: SimpleNamespace) -> None:
        self.user = user

    def scalar_one_or_none(self) -> SimpleNamespace:
        return self.user


class _FakeMasterSession:
    def __init__(self, user: SimpleNamespace) -> None:
        self.user = user
        self.committed = False

    async def execute(self, _statement):
        return _FakeResult(self.user)

    async def commit(self) -> None:
        self.committed = True

    async def refresh(self, _user: SimpleNamespace) -> None:
        return None


@pytest.fixture
def crm_list_dependency_overrides(client: TestClient, test_user_ids):
    user_ids = UserIDs(
        user_id=test_user_ids["user_id"],
        organisation_id=test_user_ids["organisation_id"],
        user_name="Test User",
        user_email="test@example.com",
    )
    user = SimpleNamespace(
        user_id=user_ids.user_id,
        organisation_id=user_ids.organisation_id,
        preferences={
            "crm": {
                "accounts": {"list": {"columns": ["name", "owner.name"]}},
                "deals": {"preferred_currency": "INR"},
            }
        },
    )
    session = _FakeMasterSession(user)

    async def _override_user():
        return user_ids

    async def _override_master_session():
        yield session

    async def _override_org_session():
        yield SimpleNamespace()

    async def _override_rate_limit():
        return None

    from src.infra.utils.app_state import fastapp as app

    previous_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_master_async_session] = _override_master_session
    app.dependency_overrides[get_org_async_session] = _override_org_session
    app.dependency_overrides[rate_limit_user_read] = _override_rate_limit
    app.dependency_overrides[rate_limit_user_write] = _override_rate_limit
    yield SimpleNamespace(user=user, session=session)
    app.dependency_overrides = previous_overrides


@pytest.mark.parametrize(
    ("entity", "sort_by", "sort_order"),
    [
        ("contacts", "last_activity_at", "desc"),
        ("accounts", "created_at", "desc"),
        ("deals", "amount", "asc"),
        ("tasks", "due_date", "desc"),
    ],
)
def test_patch_crm_list_sorting_persists_each_supported_collection(
    client: TestClient,
    crm_list_dependency_overrides,
    entity: str,
    sort_by: str,
    sort_order: str,
) -> None:
    """Each sortable CRM collection should save its own independent selection."""
    response = client.patch(
        f"/users/preference/crm/lists/{entity}/sorting",
        json={"sort_by": sort_by, "sort_order": sort_order},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"sort_by": sort_by, "sort_order": sort_order}
    assert (
        crm_list_dependency_overrides.user.preferences["crm"][entity]["list"]["sort_by"] == sort_by
    )
    assert crm_list_dependency_overrides.session.committed is True
    assert (
        crm_list_dependency_overrides.user.preferences["crm"]["deals"]["preferred_currency"]
        == "INR"
    )


def test_patch_crm_list_sorting_rejects_an_unsupported_field(
    client: TestClient,
    crm_list_dependency_overrides,
) -> None:
    """A list must not persist a sort field its read API cannot execute."""
    response = client.patch(
        "/users/preference/crm/lists/accounts/sorting",
        json={"sort_by": "pipeline_value", "sort_order": "desc"},
    )

    assert response.status_code == 422
    assert crm_list_dependency_overrides.session.committed is False


def test_patch_crm_list_sorting_preserves_existing_columns(
    client: TestClient,
    crm_list_dependency_overrides,
) -> None:
    """Saving sorting should merge into rather than replace the existing list branch."""
    response = client.patch(
        "/users/preference/crm/lists/accounts/sorting",
        json={"sort_by": "owner", "sort_order": "asc"},
    )

    assert response.status_code == 200
    assert crm_list_dependency_overrides.user.preferences["crm"]["accounts"]["list"]["columns"] == [
        "name",
        "owner.name",
    ]


def test_get_crm_list_configuration_returns_selected_then_available_columns(
    client: TestClient,
    crm_list_dependency_overrides,
) -> None:
    """The page-ready contract owns selection, ordering, and available fields."""
    response = client.get("/users/preference/crm/lists/accounts")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["entity"] == "accounts"
    assert [column["id"] for column in data["columns"][:2]] == ["name", "owner.name"]
    assert data["columns"][0] == {
        "id": "name",
        "label": "Account name",
        "data_type": "text",
        "source": "standard",
        "value_path": "name",
        "visible": True,
        "default_visible": True,
        "position": 0,
        "sortable": True,
        "hideable": False,
    }
    assert any(column["id"] == "website" and not column["visible"] for column in data["columns"])


@pytest.mark.parametrize(
    ("entity", "expected_columns"),
    [
        ("contacts", {"lifecycle_stage", "lead_status", "country"}),
        ("accounts", {"company_type", "city", "state"}),
    ],
)
def test_get_crm_list_configuration_exposes_operational_columns(
    client: TestClient,
    crm_list_dependency_overrides,
    entity: str,
    expected_columns: set[str],
) -> None:
    """List catalogs expose stored operational fields without selecting them by default."""
    response = client.get(f"/users/preference/crm/lists/{entity}")

    assert response.status_code == 200, response.text
    columns = {column["id"]: column for column in response.json()["data"]["columns"]}
    assert expected_columns <= columns.keys()
    assert all(columns[column_id]["default_visible"] is False for column_id in expected_columns)


def test_get_deal_configuration_exposes_required_stage_and_operational_columns(
    client: TestClient,
    crm_list_dependency_overrides,
    mocker,
) -> None:
    """Deal Stage remains visible while optional forecasting fields stay user-selectable."""
    mocker.patch(
        "src.service.users.crm_list_preferences_service.DealSchemaService.get_schema",
        return_value=CRMEntitySchemaResponse(entity="deal", fields=[]),
    )

    response = client.get("/users/preference/crm/lists/deals")

    assert response.status_code == 200, response.text
    columns = {column["id"]: column for column in response.json()["data"]["columns"]}
    assert columns["stage"]["hideable"] is False
    assert {"forecast_category", "priority", "days_in_stage"} <= columns.keys()
    assert all(
        columns[column_id]["default_visible"] is False
        for column_id in {"forecast_category", "priority", "days_in_stage"}
    )


def test_get_deal_configuration_uses_readable_internal_custom_field_ids(
    client: TestClient,
    crm_list_dependency_overrides,
    mocker,
) -> None:
    """Internal custom keys are stable public IDs; provider fields are not list columns."""
    crm_list_dependency_overrides.user.preferences["crm"]["deals"]["list"] = {
        "columns": ["name", "custom_columns.procurement_stage", "stale_column"]
    }
    mocker.patch(
        "src.service.users.crm_list_preferences_service.DealSchemaService.get_schema",
        return_value=CRMEntitySchemaResponse(
            entity="deal",
            fields=[
                CRMFieldSchemaItem(
                    field_id=None,
                    entity="deal",
                    key="procurement_stage",
                    label="Procurement stage",
                    type="select",
                    order=1000,
                    source="internal",
                    ownership="internal",
                ),
                CRMFieldSchemaItem(
                    field_id=None,
                    entity="deal",
                    key="provider_score",
                    label="Provider score",
                    type="number",
                    order=1001,
                    source="provider",
                    ownership="hubspot",
                ),
            ],
        ),
    )

    response = client.get("/users/preference/crm/lists/deals")

    assert response.status_code == 200, response.text
    columns = response.json()["data"]["columns"]
    assert [column["id"] for column in columns[:3]] == [
        "name",
        "stage",
        "custom_columns.procurement_stage",
    ]
    assert columns[1]["hideable"] is False
    assert columns[1]["visible"] is True
    assert columns[2]["visible"] is True
    assert columns[2]["source"] == "custom"
    assert not any(
        column["id"] in {"stale_column", "custom_columns.provider_score"} for column in columns
    )


def test_put_crm_list_columns_persists_order_without_replacing_siblings(
    client: TestClient,
    crm_list_dependency_overrides,
) -> None:
    """A drag result replaces only one list's ordered columns branch."""
    response = client.put(
        "/users/preference/crm/lists/accounts",
        json={"columns": ["name", "industry", "domain"]},
    )

    assert response.status_code == 200, response.text
    assert [column["id"] for column in response.json()["data"]["columns"] if column["visible"]] == [
        "name",
        "industry",
        "domain",
    ]
    assert crm_list_dependency_overrides.user.preferences["crm"]["accounts"]["list"] == {
        "columns": ["name", "industry", "domain"]
    }
    assert (
        crm_list_dependency_overrides.user.preferences["crm"]["deals"]["preferred_currency"]
        == "INR"
    )
    assert crm_list_dependency_overrides.session.committed is True


def test_put_deal_columns_rejects_removing_stage(
    client: TestClient,
    crm_list_dependency_overrides,
    mocker,
) -> None:
    """Deal identity and workflow state are mandatory even for direct API callers."""
    mocker.patch(
        "src.service.users.crm_list_preferences_service.DealSchemaService.get_schema",
        return_value=CRMEntitySchemaResponse(entity="deal", fields=[]),
    )

    response = client.put(
        "/users/preference/crm/lists/deals",
        json={"columns": ["name", "amount"]},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "stage is required"
    assert crm_list_dependency_overrides.session.committed is False


@pytest.mark.parametrize(
    "columns",
    [
        ["industry"],
        ["name", "name"],
        ["name", "not_a_real_column"],
    ],
)
def test_put_crm_list_columns_rejects_unsafe_selections(
    client: TestClient,
    crm_list_dependency_overrides,
    columns: list[str],
) -> None:
    """Invalid, duplicate, and identity-free selections must never be persisted."""
    response = client.put(
        "/users/preference/crm/lists/accounts",
        json={"columns": columns},
    )

    assert response.status_code == 422
    assert crm_list_dependency_overrides.session.committed is False


@pytest.mark.parametrize(
    ("entity", "expected_default_columns", "required_columns"),
    [
        (
            "tasks",
            [
                "subject",
                "due_date",
                "status",
                "priority",
                "deal.name",
                "assignee.name",
                "source",
                "created_at",
            ],
            {"subject", "status"},
        ),
        (
            "leads",
            [
                "lead",
                "email",
                "company",
                "status",
                "source",
                "owner",
                "budget",
                "score",
                "captured",
            ],
            {"lead", "status"},
        ),
    ],
)
def test_get_column_configuration_supports_tasks_and_leads(
    client: TestClient,
    crm_list_dependency_overrides,
    entity: str,
    expected_default_columns: list[str],
    required_columns: set[str],
) -> None:
    """Actions and Leads expose their current table fields through the shared API."""
    response = client.get(f"/users/preference/crm/lists/{entity}")

    assert response.status_code == 200, response.text
    columns = response.json()["data"]["columns"]
    assert [column["id"] for column in columns if column["visible"]] == expected_default_columns
    assert {column["id"] for column in columns if column["hideable"] is False} == required_columns


@pytest.mark.parametrize(
    ("entity", "columns"),
    [
        ("tasks", ["subject", "status", "due_date"]),
        ("leads", ["lead", "status", "email"]),
    ],
)
def test_put_tasks_and_leads_persists_the_user_selection(
    client: TestClient,
    crm_list_dependency_overrides,
    entity: str,
    columns: list[str],
) -> None:
    """Both migrated lists save ordered choices in the authenticated user document."""
    response = client.put(
        f"/users/preference/crm/lists/{entity}",
        json={"columns": columns},
    )

    assert response.status_code == 200, response.text
    assert crm_list_dependency_overrides.user.preferences["crm"][entity]["list"] == {
        "columns": columns
    }
    assert crm_list_dependency_overrides.session.committed is True


@pytest.mark.parametrize(
    ("entity", "columns", "missing"),
    [
        ("tasks", ["subject", "due_date"], "status"),
        ("leads", ["lead", "email"], "status"),
    ],
)
def test_put_tasks_and_leads_rejects_removing_workflow_state(
    client: TestClient,
    crm_list_dependency_overrides,
    entity: str,
    columns: list[str],
    missing: str,
) -> None:
    """Direct API callers cannot remove identity or workflow state from either list."""
    response = client.put(
        f"/users/preference/crm/lists/{entity}",
        json={"columns": columns},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == f"{missing} is required"
    assert crm_list_dependency_overrides.session.committed is False
