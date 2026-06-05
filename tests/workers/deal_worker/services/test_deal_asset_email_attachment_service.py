"""Tests for email attachment → deal asset extraction service."""

from __future__ import annotations

from unittest.mock import patch
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.elements import ClauseElement
from src.domain.models.organisation_tables.deal_asset_models import (
    DealAssetDirection,
)
from src.domain.models.organisation_tables.sync.email_models import (
    DealEmailDirection,
)
from src.domain.models.organisation_tables.sync.integration_models import IntegrationType
from src.workers.deal_worker.services.deal_asset_email_attachment_service import (
    DealAssetEmailAttachmentService,
    _build_attachment_source_record_id,
    _build_storage_path,
    _email_has_attachments_filter,
    _is_gmail,
    _is_supported_attachment_mimetype,
    _map_email_direction_to_asset_direction,
    _normalize_mimetype,
    _safe_asset_filename,
)


class _FakeExecuteResult:
    def all(self) -> list[object]:
        return []


class _CapturingSession:
    def __init__(self) -> None:
        self.statements: list[ClauseElement] = []

    async def execute(self, stmt: ClauseElement) -> _FakeExecuteResult:
        self.statements.append(stmt)
        return _FakeExecuteResult()


def _compile_sql(stmt: ClauseElement) -> str:
    return str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


class TestBuildStoragePath:
    def test_generates_expected_path_structure(self):
        deal_id = UUID("12345678-1234-1234-1234-123456789abc")
        internet_msg_id = "<CABcDeFgH@mail.gmail.com>"
        filename = "My Proposal (v2).pdf"

        path = _build_storage_path(deal_id, internet_msg_id, "att-1", filename)

        assert path.startswith(
            "deal_assets/12345678-1234-1234-1234-123456789abc/email_attachments/"
        )
        # Filename sanitized — no parens or spaces
        assert "(" not in path
        assert " " not in path.split("/")[-1]
        assert path.endswith(".pdf")

    def test_different_message_ids_produce_different_paths(self):
        deal_id = UUID("12345678-1234-1234-1234-123456789abc")

        path1 = _build_storage_path(deal_id, "<msg1@x.com>", "att-1", "file.pdf")
        path2 = _build_storage_path(deal_id, "<msg2@x.com>", "att-1", "file.pdf")

        assert path1 != path2

    def test_different_attachment_ids_produce_different_paths(self):
        deal_id = UUID("12345678-1234-1234-1234-123456789abc")

        path1 = _build_storage_path(deal_id, "<msg@x.com>", "att-1", "file.pdf")
        path2 = _build_storage_path(deal_id, "<msg@x.com>", "att-2", "file.pdf")

        assert path1 != path2

    def test_same_inputs_produce_same_path(self):
        deal_id = UUID("12345678-1234-1234-1234-123456789abc")

        path1 = _build_storage_path(deal_id, "<msg@x.com>", "att-1", "file.pdf")
        path2 = _build_storage_path(deal_id, "<msg@x.com>", "att-1", "file.pdf")

        assert path1 == path2

    def test_empty_filename_gets_fallback(self):
        deal_id = UUID("12345678-1234-1234-1234-123456789abc")

        path = _build_storage_path(deal_id, "<msg@x.com>", "att-1", "")

        assert path.endswith("/attachment")

    def test_storage_filename_component_is_bounded(self):
        deal_id = UUID("12345678-1234-1234-1234-123456789abc")
        long_filename = ("a" * 400) + ".pdf"

        path = _build_storage_path(deal_id, "<msg@x.com>", "att-1", long_filename)

        assert len(path.split("/")[-1]) <= 128
        assert path.endswith(".pdf")


class TestSafeAssetFilename:
    def test_empty_filename_gets_fallback(self):
        assert _safe_asset_filename("") == "attachment"

    def test_long_filename_is_bounded_and_keeps_extension(self):
        result = _safe_asset_filename(("a" * 400) + ".pdf")

        assert len(result) <= 128
        assert result.endswith(".pdf")


class TestBuildAttachmentSourceRecordId:
    def test_source_id_is_per_attachment(self):
        source1 = _build_attachment_source_record_id("<msg@x.com>", "att-1")
        source2 = _build_attachment_source_record_id("<msg@x.com>", "att-2")

        assert source1 != source2

    def test_source_id_is_stable(self):
        source1 = _build_attachment_source_record_id("<msg@x.com>", "att-1")
        source2 = _build_attachment_source_record_id("<msg@x.com>", "att-1")

        assert source1 == source2


class TestAttachmentMimetypePolicy:
    def test_normalizes_content_type_parameters(self):
        assert _normalize_mimetype("Application/PDF; charset=binary") == "application/pdf"

    def test_allows_supported_mimetype(self):
        assert _is_supported_attachment_mimetype("application/pdf")

    def test_rejects_active_html_mimetype(self):
        assert not _is_supported_attachment_mimetype("text/html")

    def test_rejects_blank_mimetype(self):
        assert not _is_supported_attachment_mimetype(None)


class TestDirectionMapping:
    def test_inbound_maps_to_received(self):
        assert (
            _map_email_direction_to_asset_direction(DealEmailDirection.INBOUND)
            == DealAssetDirection.RECEIVED.value
        )

    def test_outbound_maps_to_shared(self):
        assert (
            _map_email_direction_to_asset_direction(DealEmailDirection.OUTBOUND)
            == DealAssetDirection.SHARED.value
        )

    def test_internal_maps_to_internal(self):
        assert (
            _map_email_direction_to_asset_direction(DealEmailDirection.INTERNAL)
            == DealAssetDirection.INTERNAL.value
        )

    def test_accepts_raw_string_value(self):
        assert (
            _map_email_direction_to_asset_direction("INBOUND") == DealAssetDirection.RECEIVED.value
        )

    def test_unknown_direction_defaults_to_internal(self):
        assert (
            _map_email_direction_to_asset_direction("UNKNOWN") == DealAssetDirection.INTERNAL.value
        )


class TestEmailAttachmentQueryFilters:
    def test_has_attachments_filter_matches_string_backed_column(self):
        compiled = str(
            _email_has_attachments_filter().compile(compile_kwargs={"literal_binds": True})
        )

        assert "emails.has_attachments = 'true'" in compiled

    def test_google_provider_parent_uses_gmail_attachment_api(self):
        assert _is_gmail(IntegrationType.GOOGLE) is True

    @pytest.mark.asyncio
    async def test_email_extraction_query_uses_provider_parent_integrations(self):
        service = DealAssetEmailAttachmentService(organisation_id=UUID(int=1))
        session = _CapturingSession()

        async def fake_acquire(_org_id):
            yield session

        with patch(
            "src.workers.deal_worker.services.deal_asset_email_attachment_service.acquire_org_async_session",
            side_effect=fake_acquire,
        ):
            emails = await service._find_emails_needing_extraction(UUID(int=2))  # noqa: SLF001

        assert emails == []
        stmt = _compile_sql(session.statements[0])
        assert "'google'" in stmt
        assert "'microsoft'" in stmt
        assert "'gmail'" not in stmt
        assert "'outlook_email'" not in stmt
