"""Tests for DealImportProcessingService payload building."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from src.service.crm.internal.deal_write_service import DealWriteServiceError
from src.workers.deal_worker.services.deal_import_service import (
    ActorInfo,
    CustomFieldRuntime,
    DealImportContext,
    DealImportProcessingService,
)


class DummyPublisher:
    def publish_progress(self, *_args, **_kwargs) -> None:
        return None


def _service(
    *,
    import_mode: str = "upsert",
    strict_validation: bool = False,
) -> DealImportProcessingService:
    return DealImportProcessingService(
        organisation_id=uuid4(),
        actor_user_id=uuid4(),
        storage_path="crm_imports/deals/test.csv",
        publisher=DummyPublisher(),
        import_mode=import_mode,
        strict_validation=strict_validation,
    )


def _context(pipeline, stage=None) -> DealImportContext:
    stage_by_name = {}
    if stage is not None:
        stage_by_name[stage.name.lower()] = stage
    return DealImportContext(
        integration_id=uuid4(),
        default_pipeline_id=pipeline.pipeline_id,
        pipeline=pipeline,
        stage_by_name=stage_by_name,
        custom_fields_by_key={},
    )


def test_build_payload_uses_company_fallback_and_default_pipeline() -> None:
    service = _service()
    pipeline = SimpleNamespace(pipeline_id=uuid4(), name="Default Pipeline", label=None)
    context = _context(pipeline)

    row = {
        "deal_name": "",
        "company_name": "Acme Corp",
        "company_domain": "acme.com",
    }

    payload, warnings = service._build_payload(row, context, user_map={})

    assert payload["name"] == "Acme Corp"
    assert payload["pipeline_id"] == pipeline.pipeline_id
    assert payload["stage_id"] is None
    assert warnings == []


def test_build_payload_owner_email_missing_raises() -> None:
    service = _service()
    pipeline = SimpleNamespace(pipeline_id=uuid4(), name="Pipeline", label=None)
    context = _context(pipeline)

    row = {"deal_name": "Deal 1", "owner_email": "missing@acme.com"}

    with pytest.raises(DealWriteServiceError) as exc:
        service._build_payload(row, context, user_map={})

    assert "Owner not found" in exc.value.message


def test_build_payload_owner_defaults_to_importer_when_email_missing() -> None:
    service = _service()
    pipeline = SimpleNamespace(pipeline_id=uuid4(), name="Pipeline", label=None)
    context = _context(pipeline)

    row = {"deal_name": "Deal Without Owner"}

    payload, warnings = service._build_payload(row, context, user_map={})

    assert payload["owner_user_id"] == service.actor_user_id
    assert payload["owner_email_provided"] is False
    assert warnings == [{"warning": "owner_defaulted_to_importer"}]


@pytest.mark.asyncio
async def test_update_deal_preserves_existing_owner_when_owner_email_missing() -> None:
    service = _service()
    existing_owner_id = uuid4()
    pipeline = SimpleNamespace(pipeline_id=uuid4(), name="Pipeline", label=None, external_id="p1")
    existing_deal = SimpleNamespace(
        deal_id=uuid4(),
        owner_id=existing_owner_id,
        stage_id=None,
        pipeline_id=pipeline.pipeline_id,
    )
    context = _context(pipeline)

    class _FakeDealWriteService:
        def __init__(self):
            self.assign_owner_called = False

        async def update_deal(self, _deal_id, _payload):
            return existing_deal

        async def assign_owner(self, _deal_id, _owner_user_id):
            self.assign_owner_called = True
            return existing_deal

    class _FakeOrgSession:
        async def commit(self):
            return None

        async def refresh(self, _deal):
            return None

        async def rollback(self):
            return None

    fake_service = _FakeDealWriteService()
    fake_session = _FakeOrgSession()

    payload = {
        "name": "Deal Updated",
        "owner_user_id": service.actor_user_id,
        "owner_email_provided": False,
    }

    actor_info = ActorInfo(
        user_id=service.actor_user_id,
        user_name="Test User",
        user_email="test@example.com",
    )
    await service._update_deal(
        existing_deal=existing_deal,
        payload=payload,
        context=context,
        service=fake_service,
        org_session=fake_session,
        actor_info=actor_info,
    )

    assert fake_service.assign_owner_called is False


@pytest.mark.asyncio
async def test_update_deal_assigns_owner_when_owner_email_provided() -> None:
    service = _service()
    pipeline = SimpleNamespace(pipeline_id=uuid4(), name="Pipeline", label=None, external_id="p1")
    existing_deal = SimpleNamespace(
        deal_id=uuid4(),
        owner_id=uuid4(),
        stage_id=None,
        pipeline_id=pipeline.pipeline_id,
    )
    context = _context(pipeline)

    class _FakeDealWriteService:
        def __init__(self):
            self.assign_owner_called = False

        async def update_deal(self, _deal_id, _payload):
            return existing_deal

        async def assign_owner(self, _deal_id, _owner_user_id):
            self.assign_owner_called = True
            existing_deal.owner_id = _owner_user_id
            return existing_deal

    class _FakeOrgSession:
        async def commit(self):
            return None

        async def refresh(self, _deal):
            return None

        async def rollback(self):
            return None

    fake_service = _FakeDealWriteService()
    fake_session = _FakeOrgSession()
    new_owner_id = service.actor_user_id

    payload = {
        "name": "Deal Updated",
        "owner_user_id": new_owner_id,
        "owner_email_provided": True,
    }

    actor_info = ActorInfo(
        user_id=service.actor_user_id,
        user_name="Test User",
        user_email="test@example.com",
    )
    await service._update_deal(
        existing_deal=existing_deal,
        payload=payload,
        context=context,
        service=fake_service,
        org_session=fake_session,
        actor_info=actor_info,
    )

    assert fake_service.assign_owner_called is True
    assert existing_deal.owner_id == new_owner_id


@pytest.mark.asyncio
async def test_update_deal_assigns_importer_when_owner_missing_and_existing_owner_empty() -> None:
    service = _service()
    pipeline = SimpleNamespace(pipeline_id=uuid4(), name="Pipeline", label=None, external_id="p1")
    existing_deal = SimpleNamespace(
        deal_id=uuid4(),
        owner_id=None,
        stage_id=None,
        pipeline_id=pipeline.pipeline_id,
    )
    context = _context(pipeline)

    class _FakeDealWriteService:
        def __init__(self):
            self.assign_owner_called = False

        async def update_deal(self, _deal_id, _payload):
            return existing_deal

        async def assign_owner(self, _deal_id, _owner_user_id):
            self.assign_owner_called = True
            existing_deal.owner_id = _owner_user_id
            return existing_deal

    class _FakeOrgSession:
        async def commit(self):
            return None

        async def refresh(self, _deal):
            return None

        async def rollback(self):
            return None

    fake_service = _FakeDealWriteService()
    fake_session = _FakeOrgSession()
    fallback_owner_id = service.actor_user_id

    payload = {
        "name": "Deal Updated",
        "owner_user_id": fallback_owner_id,
        "owner_email_provided": False,
    }

    actor_info = ActorInfo(
        user_id=service.actor_user_id,
        user_name="Test User",
        user_email="test@example.com",
    )
    await service._update_deal(
        existing_deal=existing_deal,
        payload=payload,
        context=context,
        service=fake_service,
        org_session=fake_session,
        actor_info=actor_info,
    )

    assert fake_service.assign_owner_called is True
    assert existing_deal.owner_id == fallback_owner_id


def test_build_payload_stage_not_found_raises() -> None:
    service = _service()
    pipeline = SimpleNamespace(pipeline_id=uuid4(), name="Pipeline", label=None)
    context = _context(pipeline, stage=None)

    row = {
        "deal_name": "Deal 2",
        "stage_name": "Qualification",
    }

    with pytest.raises(DealWriteServiceError) as exc:
        service._build_payload(row, context, user_map={})

    assert "Stage not found" in exc.value.message


def test_build_payload_team_member_warning() -> None:
    service = _service()
    pipeline = SimpleNamespace(pipeline_id=uuid4(), name="Pipeline", label=None)
    context = _context(pipeline)

    user_id = uuid4()
    user_map = {"owner@acme.com": user_id}
    row = {
        "deal_name": "Deal 3",
        "team_member_emails": "owner@acme.com,missing@acme.com",
    }

    payload, warnings = service._build_payload(row, context, user_map=user_map)

    assert payload["team_members"] == [{"user_id": user_id, "role": "member"}]
    assert warnings == [{"email": "missing@acme.com", "warning": "team_member_not_found"}]


def test_build_payload_invalid_close_date_raises() -> None:
    service = _service()
    pipeline = SimpleNamespace(pipeline_id=uuid4(), name="Pipeline", label=None)
    context = _context(pipeline)

    row = {"deal_name": "Deal 4", "close_date": "not-a-date"}

    with pytest.raises(DealWriteServiceError) as exc:
        service._build_payload(row, context, user_map={})

    assert "Invalid close_date" in exc.value.message


def test_parse_csv_rejects_rows_with_extra_columns() -> None:
    service = _service()
    content = b"deal_name,amount\nDeal A,100,unexpected\n"

    with pytest.raises(DealWriteServiceError) as exc:
        service._parse_csv(content)

    assert "too many columns" in exc.value.message


def test_parse_csv_requires_header_row() -> None:
    service = _service()

    with pytest.raises(DealWriteServiceError) as exc:
        service._parse_csv(b"")

    assert "missing a header row" in exc.value.message


def test_build_payload_invalid_amount_raises() -> None:
    service = _service()
    pipeline = SimpleNamespace(pipeline_id=uuid4(), name="Pipeline", label=None)
    context = _context(pipeline)

    with pytest.raises(DealWriteServiceError) as exc:
        service._build_payload(
            {"deal_name": "Deal 5", "amount": "not-a-number"}, context, user_map={}
        )

    assert "Invalid amount" in exc.value.message


def test_clean_value_handles_list_values() -> None:
    service = _service()

    value = service._clean_value([" one ", None, "two", "  "])

    assert value == "one,two"


def test_extract_custom_properties_uses_custom_prefix_only() -> None:
    service = _service()

    row = {
        "custom_client_relationship_type": "Existing Client – Upsell",
        "custom_margin": "67%",
        "lead_quality": "Highly Qualified",
        "custom_empty_value": "   ",
    }

    props, warnings = service._extract_custom_properties(row=row, custom_fields_by_key={})

    assert props == {
        "client_relationship_type": "Existing Client – Upsell",
        "margin": "67%",
    }
    assert warnings == []


def test_build_payload_maps_company_industry_and_geography() -> None:
    service = _service()
    pipeline = SimpleNamespace(pipeline_id=uuid4(), name="Pipeline", label=None)
    context = _context(pipeline)

    row = {
        "deal_name": "Deal Geo",
        "company_name": "Acme",
        "company_domain": "acme.com",
        "company_industry": "Powerline",
        "geography": "India",
    }

    payload, _warnings = service._build_payload(row, context, user_map={})

    assert payload["account"] == {
        "domain": "acme.com",
        "name": "Acme",
        "website": None,
        "linkedin_url": None,
        "industry": "Powerline",
        "country": "India",
    }


def test_extract_custom_properties_aliases_legacy_revenue_key() -> None:
    service = _service()

    row = {
        "custom_deepmatrix_revenue_inr": "1000000",
        "custom_our_revenue_inr": "2000000",
    }

    props, warnings = service._extract_custom_properties(row=row, custom_fields_by_key={})

    assert props == {"our_revenue_inr": "2000000"}
    assert warnings == []


def test_extract_custom_properties_casts_typed_values() -> None:
    service = _service()

    row = {
        "custom_our_revenue_inr": "2000000",
        "custom_is_strategic": "yes",
        "custom_start_date": "2026-01-01",
        "custom_contract_signed_at": "2026-01-01T10:15:00+05:30",
        "custom_project_budget": "INR 1,250,000.50",
    }
    custom_fields = {
        "our_revenue_inr": CustomFieldRuntime(data_type="integer", validation={}),
        "is_strategic": CustomFieldRuntime(data_type="boolean", validation={}),
        "start_date": CustomFieldRuntime(data_type="date", validation={}),
        "contract_signed_at": CustomFieldRuntime(data_type="datetime", validation={}),
        "project_budget": CustomFieldRuntime(data_type="currency", validation={}),
    }

    props, warnings = service._extract_custom_properties(
        row=row, custom_fields_by_key=custom_fields
    )

    assert props["our_revenue_inr"] == 2000000
    assert props["is_strategic"] is True
    assert props["start_date"] == "2026-01-01T00:00:00+00:00"
    assert props["contract_signed_at"] == "2026-01-01T10:15:00+05:30"
    assert props["project_budget"] == 1250000.50
    assert warnings == []


def test_extract_custom_properties_falls_back_on_type_cast_error_when_not_strict() -> None:
    service = _service(strict_validation=False)
    row = {"custom_target_count": "not-an-int"}
    custom_fields = {
        "target_count": CustomFieldRuntime(data_type="integer", validation={}),
    }

    props, warnings = service._extract_custom_properties(
        row=row, custom_fields_by_key=custom_fields
    )

    assert props["target_count"] == "not-an-int"
    assert warnings and warnings[0]["warning"] == "custom_column_cast_fallback"


def test_extract_custom_properties_raises_on_type_cast_error_when_strict() -> None:
    service = _service(strict_validation=True)
    row = {"custom_target_count": "not-an-int"}
    custom_fields = {
        "target_count": CustomFieldRuntime(data_type="integer", validation={}),
    }

    with pytest.raises(DealWriteServiceError) as exc:
        service._extract_custom_properties(row=row, custom_fields_by_key=custom_fields)

    assert "Invalid integer for custom field 'target_count'" in exc.value.message


def test_normalize_import_mode_rejects_unknown_value() -> None:
    with pytest.raises(DealWriteServiceError) as exc:
        _service(import_mode="invalid_mode")

    assert "Invalid import_mode" in exc.value.message


def test_select_import_action_create_only_skips_existing_deal() -> None:
    service = _service(import_mode="create_only")
    existing_deal = SimpleNamespace(deal_id=uuid4())

    action = service._select_import_action(existing_deal)

    assert action == "skip"


def test_select_import_action_update_only_requires_existing_deal() -> None:
    service = _service(import_mode="update_only")

    with pytest.raises(DealWriteServiceError) as exc:
        service._select_import_action(None)

    assert "No matching deal found for update" in exc.value.message


def test_select_import_action_upsert_updates_when_match_exists() -> None:
    service = _service(import_mode="upsert")
    existing_deal = SimpleNamespace(deal_id=uuid4())

    action = service._select_import_action(existing_deal)

    assert action == "update"


def test_normalize_company_name_collapses_symbols_and_whitespace() -> None:
    service = _service()

    normalized = service._normalize_company_name("  ACME,   Inc.  ")

    assert normalized == "acme inc"


def test_normalize_contact_name_collapses_symbols_and_whitespace() -> None:
    service = _service()

    normalized = service._normalize_contact_name("  John,   Doe  ")

    assert normalized == "john doe"


@pytest.mark.asyncio
async def test_resolve_account_warnings_flags_ambiguous_name_only_account() -> None:
    service = _service()
    pipeline = SimpleNamespace(pipeline_id=uuid4(), name="Pipeline", label=None)
    context = _context(pipeline)

    class _FakeScalars:
        def __init__(self, values):
            self._values = values

        def all(self):
            return self._values

    class _FakeResult:
        def __init__(self, values):
            self._values = values

        def scalars(self):
            return _FakeScalars(self._values)

    class _FakeSession:
        async def execute(self, _stmt):
            return _FakeResult([uuid4(), uuid4()])

    warnings = await service._resolve_account_warnings(
        org_session=_FakeSession(),
        context=context,
        payload={"account": {"name": "Acme", "domain": None}},
    )

    assert warnings == [
        {
            "warning": "ambiguous_account_name",
            "company_name": "Acme",
            "existing_matches": 2,
        }
    ]


@pytest.mark.asyncio
async def test_resolve_account_warnings_ignores_non_ambiguous_name_only_account() -> None:
    service = _service()
    pipeline = SimpleNamespace(pipeline_id=uuid4(), name="Pipeline", label=None)
    context = _context(pipeline)

    class _FakeScalars:
        def __init__(self, values):
            self._values = values

        def all(self):
            return self._values

    class _FakeResult:
        def __init__(self, values):
            self._values = values

        def scalars(self):
            return _FakeScalars(self._values)

    class _FakeSession:
        async def execute(self, _stmt):
            return _FakeResult([uuid4()])

    warnings = await service._resolve_account_warnings(
        org_session=_FakeSession(),
        context=context,
        payload={"account": {"name": "Acme", "domain": None}},
    )

    assert warnings == []


@pytest.mark.asyncio
async def test_resolve_contact_warnings_flags_name_only_contact() -> None:
    service = _service()
    pipeline = SimpleNamespace(pipeline_id=uuid4(), name="Pipeline", label=None)
    context = _context(pipeline)

    class _FakeScalars:
        def __init__(self, values):
            self._values = values

        def all(self):
            return self._values

    class _FakeResult:
        def __init__(self, values):
            self._values = values

        def scalars(self):
            return _FakeScalars(self._values)

    class _FakeSession:
        async def execute(self, _stmt):
            return _FakeResult([uuid4()])

    warnings = await service._resolve_contact_warnings(
        org_session=_FakeSession(),
        context=context,
        payload={"primary_contact_name": "Jane Doe", "primary_contact_email": None},
    )

    assert warnings == [{"warning": "contact_email_missing", "contact_name": "Jane Doe"}]


@pytest.mark.asyncio
async def test_resolve_contact_warnings_flags_ambiguous_name_only_contact() -> None:
    service = _service()
    pipeline = SimpleNamespace(pipeline_id=uuid4(), name="Pipeline", label=None)
    context = _context(pipeline)

    class _FakeScalars:
        def __init__(self, values):
            self._values = values

        def all(self):
            return self._values

    class _FakeResult:
        def __init__(self, values):
            self._values = values

        def scalars(self):
            return _FakeScalars(self._values)

    class _FakeSession:
        async def execute(self, _stmt):
            return _FakeResult([uuid4(), uuid4()])

    warnings = await service._resolve_contact_warnings(
        org_session=_FakeSession(),
        context=context,
        payload={"primary_contact_name": "Jane Doe", "primary_contact_email": None},
    )

    assert warnings == [
        {"warning": "contact_email_missing", "contact_name": "Jane Doe"},
        {"warning": "ambiguous_contact_name", "contact_name": "Jane Doe", "existing_matches": 2},
    ]


@pytest.mark.asyncio
async def test_resolve_contact_warnings_ignores_email_contacts() -> None:
    service = _service()
    pipeline = SimpleNamespace(pipeline_id=uuid4(), name="Pipeline", label=None)
    context = _context(pipeline)

    class _FakeSession:
        async def execute(self, _stmt):
            raise AssertionError("DB query should not run when contact email is present")

    warnings = await service._resolve_contact_warnings(
        org_session=_FakeSession(),
        context=context,
        payload={"primary_contact_name": "Jane Doe", "primary_contact_email": "jane@acme.com"},
    )

    assert warnings == []
