"""Tests for email attachment → deal asset extraction service."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import httpx
import pytest
import src.service.crm.assets.email_attachment_service as attachment_service
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.elements import ClauseElement
from src.domain.models.organisation_tables.deal_asset_models import (
    DealAssetDirection,
    DealAssetStatus,
)
from src.domain.models.organisation_tables.sync.email_models import (
    DealEmailDirection,
)
from src.domain.models.organisation_tables.sync.integration_models import IntegrationType
from src.service.crm.assets.email_attachment_service import (
    DealAssetEmailAttachmentService,
    _build_attachment_source_record_id,
    _build_legacy_attachment_source_record_id,
    _build_storage_path,
    _email_has_attachments_filter,
    _EmailWithAttachments,
    _is_gmail,
    _is_missing_provider_message,
    _is_supported_attachment_mimetype,
    _map_email_direction_to_asset_direction,
    _normalize_mimetype,
    _safe_asset_filename,
)
from src.service.integrations.email.attachment_provider import (
    AttachmentMeta,
    DownloadedAttachment,
)
from src.service.integrations.providers.token_resolution import IntegrationTokenInfo


class _FakeExecuteResult:
    def all(self) -> list[object]:
        return []


class _CapturingSession:
    def __init__(self) -> None:
        self.statements: list[ClauseElement] = []

    async def execute(self, stmt: ClauseElement) -> _FakeExecuteResult:
        self.statements.append(stmt)
        return _FakeExecuteResult()


class _ConflictSafeInsertResult:
    def first(self) -> None:
        return None

    def scalar_one_or_none(self) -> None:
        return None


class _ConflictSafeInsertSession:
    def __init__(self) -> None:
        self.commit_count = 0
        self.statements: list[ClauseElement] = []

    async def execute(self, stmt: ClauseElement) -> _ConflictSafeInsertResult:
        self.statements.append(stmt)
        return _ConflictSafeInsertResult()

    async def commit(self) -> None:
        self.commit_count += 1


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
    def test_source_id_uses_filename_and_content(self):
        source1 = _build_attachment_source_record_id(
            "<msg@x.com>",
            "proposal.pdf",
            "a" * 64,
        )
        source2 = _build_attachment_source_record_id(
            "<msg@x.com>",
            "proposal.pdf",
            "b" * 64,
        )

        assert source1 != source2

    def test_source_id_is_stable_across_mailbox_local_ids(self):
        source1 = _build_attachment_source_record_id(
            "<msg@x.com>",
            "Proposal (Final).PDF",
            "a" * 64,
        )
        source2 = _build_attachment_source_record_id(
            "<msg@x.com>",
            "proposal_final_.pdf",
            "a" * 64,
        )

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
            "src.service.crm.assets.email_attachment_service.acquire_org_async_session",
            side_effect=fake_acquire,
        ):
            emails = await service._find_emails_needing_extraction(  # noqa: SLF001
                UUID(int=2),
                email_ids=[UUID(int=3)],
            )

        assert emails == []
        stmt = _compile_sql(session.statements[0])
        assert "'google'" in stmt
        assert "'microsoft'" in stmt
        assert "'gmail'" not in stmt
        assert "'outlook_email'" not in stmt
        assert "emails_1.email_id IN" in stmt
        assert "00000000-0000-0000-0000-000000000003" in stmt
        assert "emails.is_deleted IS false" in stmt
        assert "DISTINCT ON" not in stmt
        assert "ORDER BY emails.internet_message_id, emails.integration_id, emails.email_id" in stmt


def test_missing_provider_message_excludes_attachment_download_404() -> None:
    """Only a missing message should suppress future mailbox-copy fetches."""
    message_request = httpx.Request("GET", "https://gmail.test/messages/message-id")
    attachment_request = httpx.Request(
        "GET",
        "https://gmail.test/messages/message-id/attachments/attachment-id",
    )

    assert _is_missing_provider_message(
        httpx.HTTPStatusError(
            "missing",
            request=message_request,
            response=httpx.Response(404, request=message_request),
        )
    )
    assert not _is_missing_provider_message(
        httpx.HTTPStatusError(
            "missing",
            request=attachment_request,
            response=httpx.Response(404, request=attachment_request),
        )
    )


@pytest.mark.asyncio
async def test_process_deal_forwards_exact_email_scope() -> None:
    """Automatic extraction must not widen one hydration batch to the whole deal."""
    service = DealAssetEmailAttachmentService(organisation_id=UUID(int=1))
    email_id = UUID(int=3)
    service._find_emails_needing_extraction = AsyncMock(return_value=[])  # type: ignore[method-assign]
    service._close_storage = AsyncMock()  # type: ignore[method-assign]

    await service.process_deal(UUID(int=2), email_ids=[email_id])

    service._find_emails_needing_extraction.assert_awaited_once_with(  # type: ignore[attr-defined]
        UUID(int=2),
        email_ids=[email_id],
    )


@pytest.mark.asyncio
async def test_process_organisation_returns_committed_and_bounded_followup_work() -> None:
    """The org service reports orchestration work instead of publishing it."""
    service = DealAssetEmailAttachmentService(organisation_id=UUID(int=1))
    deal_id = UUID(int=2)
    asset_id = UUID(int=3)
    email_id = UUID(int=4)
    retryable_email_id = UUID(int=5)
    service._find_deals_with_unprocessed_attachments = AsyncMock(  # type: ignore[method-assign]
        return_value=[deal_id]
    )
    service._process_deal_internal = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "assets_created": 1,
            "errors": 2,
            "retryable_email_ids": [str(retryable_email_id)],
            "post_upload_asset_ids": [str(asset_id)],
            "limit_reached": True,
            "remaining_email_ids": [str(email_id)],
        }
    )
    service._close_storage = AsyncMock()  # type: ignore[method-assign]

    result = await service.process_organisation()

    assert result["post_upload_asset_ids"] == [str(asset_id)]
    assert result["continuations"] == [{"deal_id": str(deal_id), "email_ids": [str(email_id)]}]
    assert result["retry_continuations"] == [
        {
            "deal_id": str(deal_id),
            "email_ids": [str(retryable_email_id)],
        }
    ]
    assert result["unscoped_errors"] == 1


@pytest.mark.asyncio
async def test_canonical_message_falls_back_to_second_mailbox_copy() -> None:
    """One inaccessible mailbox must not hide an organisation-owned message copy."""
    organisation_id = UUID(int=1)
    deal_id = UUID(int=2)
    shared = {
        "internet_message_id": "<message@example.com>",
        "subject": "Proposal",
        "integration_type": IntegrationType.GOOGLE,
        "token_info": IntegrationTokenInfo(),
        "direction": DealEmailDirection.INBOUND.value,
        "deal_id": deal_id,
    }
    copies = [
        _EmailWithAttachments(
            email_id=UUID(int=index),
            external_message_id=f"provider-message-{index}",
            integration_id=UUID(int=index + 10),
            **shared,
        )
        for index in (3, 4)
    ]
    asset_id = UUID(int=8)
    service = DealAssetEmailAttachmentService(organisation_id)
    service._find_emails_needing_extraction = AsyncMock(return_value=copies)  # type: ignore[method-assign]
    service._process_single_email = AsyncMock(  # type: ignore[method-assign]
        side_effect=[RuntimeError("mailbox unavailable"), (1, 0, [asset_id], False)]
    )

    result = await service._process_deal_internal(  # noqa: SLF001
        deal_id,
        email_ids=[copies[0].email_id],
    )

    assert result["assets_created"] == 1
    assert result["errors"] == 0
    assert result["post_upload_asset_ids"] == [str(asset_id)]
    assert service._process_single_email.await_count == 2  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_gmail_404_is_a_terminal_mailbox_skip_not_a_retryable_error() -> None:
    """A missing provider message must not retry the whole deal attachment batch."""
    organisation_id = UUID(int=1)
    deal_id = UUID(int=2)
    email = _EmailWithAttachments(
        email_id=UUID(int=3),
        external_message_id="missing-provider-message",
        internet_message_id="<message@example.com>",
        subject="Proposal",
        integration_id=UUID(int=4),
        integration_type=IntegrationType.GOOGLE,
        token_info=IntegrationTokenInfo(),
        direction=DealEmailDirection.INBOUND.value,
        deal_id=deal_id,
    )
    request = httpx.Request(
        "GET",
        f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{email.external_message_id}",
    )
    response = httpx.Response(404, request=request)
    service = DealAssetEmailAttachmentService(organisation_id)
    service._find_emails_needing_extraction = AsyncMock(return_value=[email])  # type: ignore[method-assign]
    service._process_single_email = AsyncMock(  # type: ignore[method-assign]
        side_effect=httpx.HTTPStatusError("missing", request=request, response=response)
    )
    service._record_email_attachment_fetch_status = AsyncMock()  # type: ignore[method-assign]

    result = await service._process_deal_internal(  # noqa: SLF001
        deal_id,
        email_ids=[email.email_id],
    )

    assert result["errors"] == 0
    assert result["permanent_skips"] == 1
    assert result["retryable_email_ids"] == []
    assert result["terminal_email_failures"] == [
        {"email_id": str(email.email_id), "reason": "not_found"}
    ]
    service._record_email_attachment_fetch_status.assert_awaited_once_with(  # type: ignore[attr-defined]
        email.email_id,
        status="not_found",
    )


@pytest.mark.asyncio
async def test_transient_mailbox_failure_retries_only_that_canonical_email() -> None:
    """Network failures remain retryable without replaying unrelated successful messages."""
    deal_id = UUID(int=2)
    email = _EmailWithAttachments(
        email_id=UUID(int=3),
        external_message_id="provider-message",
        internet_message_id="<message@example.com>",
        subject="Proposal",
        integration_id=UUID(int=4),
        integration_type=IntegrationType.GOOGLE,
        token_info=IntegrationTokenInfo(),
        direction=DealEmailDirection.INBOUND.value,
        deal_id=deal_id,
    )
    service = DealAssetEmailAttachmentService(UUID(int=1))
    service._find_emails_needing_extraction = AsyncMock(return_value=[email])  # type: ignore[method-assign]
    service._process_single_email = AsyncMock(  # type: ignore[method-assign]
        side_effect=httpx.ReadTimeout("temporary timeout")
    )

    result = await service._process_deal_internal(  # noqa: SLF001
        deal_id,
        email_ids=[email.email_id],
    )

    assert result["errors"] == 1
    assert result["permanent_skips"] == 0
    assert result["retryable_email_ids"] == [str(email.email_id)]


@pytest.mark.asyncio
async def test_invalid_provider_payload_is_terminal_not_retryable() -> None:
    """Deterministic provider data errors should not consume all Celery retries."""
    deal_id = UUID(int=2)
    email = _EmailWithAttachments(
        email_id=UUID(int=3),
        external_message_id="provider-message",
        internet_message_id="<message@example.com>",
        subject="Proposal",
        integration_id=UUID(int=4),
        integration_type=IntegrationType.GOOGLE,
        token_info=IntegrationTokenInfo(),
        direction=DealEmailDirection.INBOUND.value,
        deal_id=deal_id,
    )
    service = DealAssetEmailAttachmentService(UUID(int=1))
    service._find_emails_needing_extraction = AsyncMock(return_value=[email])  # type: ignore[method-assign]
    service._process_single_email = AsyncMock(  # type: ignore[method-assign]
        side_effect=ValueError("invalid Gmail attachment encoding")
    )

    result = await service._process_deal_internal(  # noqa: SLF001
        deal_id,
        email_ids=[email.email_id],
    )

    assert result["errors"] == 0
    assert result["permanent_skips"] == 1
    assert result["retryable_email_ids"] == []
    assert result["terminal_email_failures"] == [
        {
            "email_id": str(email.email_id),
            "reason": "invalid_provider_payload",
        }
    ]


@pytest.mark.asyncio
async def test_execution_limit_returns_unvisited_email_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The next task must receive every scoped canonical message not yet visited."""
    monkeypatch.setattr(attachment_service, "MAX_ATTACHMENTS_PER_DEAL", 1)
    deal_id = UUID(int=2)
    emails = [
        _EmailWithAttachments(
            email_id=UUID(int=index),
            external_message_id=f"provider-message-{index}",
            internet_message_id=f"<message-{index}@example.com>",
            subject="Proposal",
            integration_id=UUID(int=index + 10),
            integration_type=IntegrationType.GOOGLE,
            token_info=IntegrationTokenInfo(),
            direction=DealEmailDirection.INBOUND.value,
            deal_id=deal_id,
        )
        for index in (3, 4)
    ]
    service = DealAssetEmailAttachmentService(UUID(int=1))
    service._find_emails_needing_extraction = AsyncMock(return_value=emails)  # type: ignore[method-assign]
    service._process_single_email = AsyncMock(  # type: ignore[method-assign]
        return_value=(1, 0, [UUID(int=8)], False)
    )

    result = await service._process_deal_internal(  # noqa: SLF001
        deal_id,
        email_ids=[email.email_id for email in emails],
    )

    assert result["limit_reached"] is True
    assert result["remaining_email_ids"] == [str(emails[1].email_id)]
    assert service._process_single_email.await_count == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_mid_email_limit_requeues_same_email() -> None:
    """A continuation must relist the current message when its files were only partly read."""
    deal_id = UUID(int=2)
    email = _EmailWithAttachments(
        email_id=UUID(int=3),
        external_message_id="provider-message",
        internet_message_id="<message@example.com>",
        subject="Proposal",
        integration_id=UUID(int=4),
        integration_type=IntegrationType.GOOGLE,
        token_info=IntegrationTokenInfo(),
        direction=DealEmailDirection.INBOUND.value,
        deal_id=deal_id,
    )
    service = DealAssetEmailAttachmentService(UUID(int=1))
    service._find_emails_needing_extraction = AsyncMock(return_value=[email])  # type: ignore[method-assign]
    service._process_single_email = AsyncMock(  # type: ignore[method-assign]
        return_value=(1, 0, [UUID(int=8)], True)
    )

    result = await service._process_deal_internal(  # noqa: SLF001
        deal_id,
        email_ids=[email.email_id],
    )

    assert result["limit_reached"] is True
    assert result["remaining_email_ids"] == [str(email.email_id)]


@pytest.mark.asyncio
async def test_download_uses_content_stable_identity_and_persists_checksum() -> None:
    """Provider-local attachment IDs are provenance, not the deduplication key."""
    organisation_id = UUID(int=1)
    deal_id = UUID(int=2)
    content = b"same proposal bytes"
    checksum = hashlib.sha256(content).hexdigest()
    asset_id = UUID(int=8)
    service = DealAssetEmailAttachmentService(organisation_id)
    service.storage.upload_bytes = AsyncMock()  # type: ignore[method-assign]
    service._find_legacy_asset_by_content = AsyncMock(return_value=None)  # type: ignore[method-assign]
    service._create_deal_asset = AsyncMock(  # type: ignore[method-assign]
        return_value=(True, asset_id)
    )
    email = _EmailWithAttachments(
        email_id=UUID(int=3),
        external_message_id="provider-message",
        internet_message_id="<message@example.com>",
        subject="Proposal",
        integration_id=UUID(int=4),
        integration_type=IntegrationType.GOOGLE,
        token_info=IntegrationTokenInfo(),
        direction=DealEmailDirection.INBOUND.value,
        deal_id=deal_id,
    )
    attachment = AttachmentMeta(
        filename="proposal.pdf",
        mime_type="application/pdf",
        size_bytes=len(content),
        attachment_id="mailbox-local-id",
    )

    with patch(
        "src.service.crm.assets.email_attachment_service.download_gmail_attachment",
        new=AsyncMock(
            return_value=DownloadedAttachment(
                content=content,
                size_bytes=len(content),
            )
        ),
    ):
        result = await service._download_and_create_asset(  # noqa: SLF001
            AsyncMock(),
            email,
            attachment,
            True,
        )

    expected_source_id = _build_attachment_source_record_id(
        email.internet_message_id,
        attachment.filename,
        checksum,
    )
    assert result == (1, 0, asset_id)
    service._create_deal_asset.assert_awaited_once()  # type: ignore[attr-defined]
    assert service._create_deal_asset.await_args.args[-2:] == (  # type: ignore[attr-defined]
        expected_source_id,
        checksum,
    )


@pytest.mark.asyncio
async def test_cross_mailbox_download_reuses_matching_legacy_asset() -> None:
    """A legacy provider-local key must not duplicate the same canonical file."""
    content = b"same proposal bytes"
    existing_asset_id = UUID(int=8)
    service = DealAssetEmailAttachmentService(UUID(int=1))
    service.storage.upload_bytes = AsyncMock()  # type: ignore[method-assign]
    service._find_legacy_asset_by_content = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(
            asset_id=existing_asset_id,
            status=DealAssetStatus.ACTIVE.value,
            storage_path="deal_assets/legacy.pdf",
        )
    )
    service._create_deal_asset = AsyncMock()  # type: ignore[method-assign]
    email = _EmailWithAttachments(
        email_id=UUID(int=3),
        external_message_id="provider-message",
        internet_message_id="<message@example.com>",
        subject="Proposal",
        integration_id=UUID(int=4),
        integration_type=IntegrationType.GOOGLE,
        token_info=IntegrationTokenInfo(),
        direction=DealEmailDirection.INBOUND.value,
        deal_id=UUID(int=2),
    )
    attachment = AttachmentMeta(
        filename="proposal.pdf",
        mime_type="application/pdf",
        size_bytes=len(content),
        attachment_id="different-mailbox-local-id",
    )

    with patch(
        "src.service.crm.assets.email_attachment_service.download_gmail_attachment",
        new=AsyncMock(
            return_value=DownloadedAttachment(
                content=content,
                size_bytes=len(content),
            )
        ),
    ):
        result = await service._download_and_create_asset(  # noqa: SLF001
            AsyncMock(),
            email,
            attachment,
            True,
        )

    assert result == (0, 1, None)
    service.storage.upload_bytes.assert_not_awaited()
    service._create_deal_asset.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_legacy_lookup_matches_canonical_message_filename_and_checksum() -> None:
    """Provider provenance may differ, but verified canonical content is one asset."""
    organisation_id = UUID(int=1)
    deal_id = UUID(int=2)
    checksum = "a" * 64
    candidate = SimpleNamespace(
        asset_id=UUID(int=8),
        filename="proposal.pdf",
        checksum=checksum,
        storage_path="deal_assets/legacy.pdf",
        asset_metadata={"internet_message_id": "<message@example.com>"},
    )
    result = MagicMock()
    result.scalars.return_value.all.return_value = [candidate]
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)
    service = DealAssetEmailAttachmentService(organisation_id)

    async def fake_acquire(_org_id):
        yield session

    with patch(
        "src.service.crm.assets.email_attachment_service.acquire_org_async_session",
        side_effect=fake_acquire,
    ):
        existing = await service._find_legacy_asset_by_content(  # noqa: SLF001
            deal_id,
            "<message@example.com>",
            "Proposal.PDF",
            checksum,
        )

    assert existing is candidate


@pytest.mark.asyncio
async def test_single_mailbox_returns_post_upload_work_without_publishing() -> None:
    """The domain service reports committed work; the Celery task owns publication."""
    service = DealAssetEmailAttachmentService(UUID(int=1))
    asset_id = UUID(int=8)
    email = _EmailWithAttachments(
        email_id=UUID(int=3),
        external_message_id="provider-message",
        internet_message_id="<message@example.com>",
        subject="Proposal",
        integration_id=UUID(int=4),
        integration_type=IntegrationType.GOOGLE,
        token_info=IntegrationTokenInfo(),
        direction=DealEmailDirection.INBOUND.value,
        deal_id=UUID(int=2),
    )
    attachment = AttachmentMeta(
        filename="proposal.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        attachment_id="attachment-1",
    )
    service._find_existing_attachment_assets = AsyncMock(return_value={})  # type: ignore[method-assign]
    service._download_and_create_asset = AsyncMock(  # type: ignore[method-assign]
        return_value=(1, 0, asset_id)
    )

    with (
        patch(
            "src.service.crm.assets.email_attachment_service.get_access_token",
            new=AsyncMock(return_value="access-token"),
        ),
        patch(
            "src.service.crm.assets.email_attachment_service.list_gmail_message_attachments",
            new=AsyncMock(return_value=[attachment]),
        ),
    ):
        result = await service._process_single_email(email, remaining=50)  # noqa: SLF001

    assert result == (1, 0, [asset_id], False)


@pytest.mark.asyncio
async def test_limit_does_not_continue_for_only_unsupported_files() -> None:
    """Policy skips after the last created asset are not unfinished work."""
    service = DealAssetEmailAttachmentService(UUID(int=1))
    asset_id = UUID(int=8)
    email = _EmailWithAttachments(
        email_id=UUID(int=3),
        external_message_id="provider-message",
        internet_message_id="<message@example.com>",
        subject="Proposal",
        integration_id=UUID(int=4),
        integration_type=IntegrationType.GOOGLE,
        token_info=IntegrationTokenInfo(),
        direction=DealEmailDirection.INBOUND.value,
        deal_id=UUID(int=2),
    )
    supported = AttachmentMeta(
        filename="proposal.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        attachment_id="attachment-1",
    )
    unsupported = AttachmentMeta(
        filename="unsafe.html",
        mime_type="text/html",
        size_bytes=1024,
        attachment_id="attachment-2",
    )
    service._find_existing_attachment_assets = AsyncMock(return_value={})  # type: ignore[method-assign]
    service._download_and_create_asset = AsyncMock(  # type: ignore[method-assign]
        return_value=(1, 0, asset_id)
    )

    with (
        patch(
            "src.service.crm.assets.email_attachment_service.get_access_token",
            new=AsyncMock(return_value="access-token"),
        ),
        patch(
            "src.service.crm.assets.email_attachment_service.list_gmail_message_attachments",
            new=AsyncMock(return_value=[supported, unsupported]),
        ),
    ):
        result = await service._process_single_email(email, remaining=1)  # noqa: SLF001

    assert result == (1, 1, [asset_id], False)


@pytest.mark.asyncio
async def test_existing_processing_legacy_asset_is_returned_for_recovery() -> None:
    """A legacy dedup hit must repair post-upload instead of becoming a silent skip."""
    service = DealAssetEmailAttachmentService(UUID(int=1))
    asset_id = UUID(int=8)
    email = _EmailWithAttachments(
        email_id=UUID(int=3),
        external_message_id="provider-message",
        internet_message_id="<message@example.com>",
        subject="Proposal",
        integration_id=UUID(int=4),
        integration_type=IntegrationType.GOOGLE,
        token_info=IntegrationTokenInfo(),
        direction=DealEmailDirection.INBOUND.value,
        deal_id=UUID(int=2),
    )
    attachment = AttachmentMeta(
        filename="proposal.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        attachment_id="attachment-1",
    )
    legacy_source_id = _build_legacy_attachment_source_record_id(
        email.internet_message_id,
        attachment.attachment_id,
    )
    service._find_existing_attachment_assets = AsyncMock(  # type: ignore[method-assign]
        return_value={
            legacy_source_id: SimpleNamespace(
                asset_id=asset_id,
                status=DealAssetStatus.PROCESSING.value,
                storage_path="deal_assets/existing.pdf",
            )
        }
    )
    service._download_and_create_asset = AsyncMock()  # type: ignore[method-assign]

    with (
        patch(
            "src.service.crm.assets.email_attachment_service.get_access_token",
            new=AsyncMock(return_value="access-token"),
        ),
        patch(
            "src.service.crm.assets.email_attachment_service.list_gmail_message_attachments",
            new=AsyncMock(return_value=[attachment]),
        ),
    ):
        result = await service._process_single_email(email, remaining=50)  # noqa: SLF001

    assert result == (0, 1, [asset_id], False)
    service._download_and_create_asset.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_create_deal_asset_returns_false_when_conflict_already_inserted() -> None:
    organisation_id = UUID(int=1)
    deal_id = UUID(int=2)
    session = _ConflictSafeInsertSession()
    service = DealAssetEmailAttachmentService(organisation_id)
    email = _EmailWithAttachments(
        email_id=UUID(int=3),
        external_message_id="provider-message",
        internet_message_id="<message@example.com>",
        subject="Proposal",
        integration_id=UUID(int=4),
        integration_type=IntegrationType.GOOGLE,
        token_info=IntegrationTokenInfo(),
        direction=DealEmailDirection.INBOUND.value,
        deal_id=deal_id,
    )
    attachment = AttachmentMeta(
        filename="proposal.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        attachment_id="attachment-1",
    )

    async def fake_acquire(_org_id):
        yield session

    with patch(
        "src.service.crm.assets.email_attachment_service.acquire_org_async_session",
        side_effect=fake_acquire,
    ):
        inserted = await service._create_deal_asset(  # noqa: SLF001
            email,
            attachment,
            "deal_assets/proposal.pdf",
            attachment.size_bytes,
            "source-record-id",
            "a" * 64,
        )

    assert inserted == (False, None)
    assert session.commit_count == 1
    assert "ON CONSTRAINT uq_deal_asset_source_dedup_org" in str(session.statements[0])


@pytest.mark.asyncio
async def test_insert_conflict_returns_processing_asset_for_post_upload_recovery() -> None:
    """Retrying extraction must republish a committed asset stranded in processing."""
    organisation_id = UUID(int=1)
    deal_id = UUID(int=2)
    existing_asset_id = UUID(int=9)
    insert_result = AsyncMock()
    insert_result.first = lambda: None
    existing_result = AsyncMock()
    existing_result.scalar_one_or_none = lambda: SimpleNamespace(
        asset_id=existing_asset_id,
        status=DealAssetStatus.PROCESSING.value,
        storage_path="deal_assets/existing.pdf",
    )
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[insert_result, existing_result])
    service = DealAssetEmailAttachmentService(organisation_id)
    email = _EmailWithAttachments(
        email_id=UUID(int=3),
        external_message_id="provider-message",
        internet_message_id="<message@example.com>",
        subject="Proposal",
        integration_id=UUID(int=4),
        integration_type=IntegrationType.GOOGLE,
        token_info=IntegrationTokenInfo(),
        direction=DealEmailDirection.INBOUND.value,
        deal_id=deal_id,
    )
    attachment = AttachmentMeta(
        filename="proposal.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        attachment_id="attachment-1",
    )

    async def fake_acquire(_org_id):
        yield session

    with patch(
        "src.service.crm.assets.email_attachment_service.acquire_org_async_session",
        side_effect=fake_acquire,
    ):
        outcome = await service._create_deal_asset(  # noqa: SLF001
            email,
            attachment,
            "deal_assets/proposal.pdf",
            attachment.size_bytes,
            "source-record-id",
            "a" * 64,
        )

    assert outcome == (False, existing_asset_id)
