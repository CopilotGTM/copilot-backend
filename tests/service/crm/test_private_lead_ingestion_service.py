"""Service-level tests for private lead-ingestion key crypto and validation flow."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from src.api.schemas.private_lead_ingestion_models import (
    CreateLeadIngestionKeyRequest,
    PrivateLeadIngestionRequest,
)
from src.domain.models.master_tables import (
    PublicIngressKey,
    PublicIngressKeyStatus,
    PublicIngressKeyType,
    PublicIngressResourceType,
)
from src.domain.models.organisation_tables.sync.crm.leads import CRMLeadSourceType
from src.service.crm.leads.ingestion_auth_service import (
    PrivateLeadIngestionAuthError,
    PrivateLeadIngestionAuthService,
)
from src.service.crm.leads.ingestion_key_service import LeadIngestionKeyService
from src.service.crm.leads.private_ingestion_service import (
    PrivateLeadIngestionService,
    PrivateLeadIngestionServiceError,
)


class RecordingAsyncSession:
    """Minimal async session stub capturing commit/refresh behavior for service tests."""

    def __init__(self, *, commit_error: Exception | None = None) -> None:
        """Initialize counters used by tests to assert transaction behavior."""
        self.added: list[object] = []
        self.commits = 0
        self.refreshed: list[object] = []
        self.rollbacks = 0
        self.commit_error = commit_error

    def add(self, instance: object) -> None:
        """Record every added model instance for assertions."""
        self.added.append(instance)

    async def commit(self) -> None:
        """Record the commit call and optionally inject the configured commit failure."""
        self.commits += 1
        if self.commit_error:
            raise self.commit_error

    async def rollback(self) -> None:
        """Track rollback calls so race-recovery paths can be asserted."""
        self.rollbacks += 1

    async def refresh(self, instance: object) -> None:
        """Track refresh calls for write-path assertions."""
        self.refreshed.append(instance)

    async def close(self) -> None:
        """A no-op close to match the async session interface in tests."""
        pass


@pytest.mark.asyncio
async def test_secret_hash_verification_uses_hmac(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stored key hashes should remain salted and not contain plaintext secret material."""
    monkeypatch.setenv("LEAD_INGESTION_SECRET_PEPPER", "test-pepper")

    stored_hash = await LeadIngestionKeyService.hash_secret("cgtm_live_secret")

    assert stored_hash.startswith("hmac_sha256$")
    assert "cgtm_live_secret" not in stored_hash
    assert await LeadIngestionKeyService.verify_secret("cgtm_live_secret", stored_hash)
    assert not await LeadIngestionKeyService.verify_secret("wrong", stored_hash)


@pytest.mark.asyncio
async def test_create_key_returns_secret_and_stores_hash_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Create-key should return one-time secret and persist only hash in the key record."""
    monkeypatch.setenv("LEAD_INGESTION_SECRET_PEPPER", "test-pepper")
    org_id = uuid4()
    session = RecordingAsyncSession()
    service = LeadIngestionKeyService(
        master_session=session,  # type: ignore[arg-type]
        organisation_id=org_id,
        actor_user_id=uuid4(),
    )

    async def generate_key_id() -> str:
        """Stubbed key generator used to make this test deterministic."""
        return "skid_test123"

    service._generate_unique_key_id = generate_key_id  # type: ignore[method-assign]
    key, secret = await service.create_key(CreateLeadIngestionKeyRequest(name="Meta Ads"))

    assert key.public_key == "skid_test123"
    assert key.secret_hash
    assert key.secret_hash != secret
    assert secret.startswith("cgtm_live_")
    assert key.organisation_id == org_id
    assert key.resource_id == org_id
    assert key.key_type == PublicIngressKeyType.PRIVATE_API
    assert session.commits == 1


@pytest.mark.asyncio
async def test_create_form_private_key_scopes_key_to_lead_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Form private keys should resolve to the form resource, not the org-level direct API."""
    monkeypatch.setenv("LEAD_INGESTION_SECRET_PEPPER", "test-pepper")
    org_id = uuid4()
    form_id = uuid4()
    session = RecordingAsyncSession()
    service = LeadIngestionKeyService(
        master_session=session,  # type: ignore[arg-type]
        organisation_id=org_id,
        actor_user_id=uuid4(),
    )

    async def generate_key_id() -> str:
        """Stubbed key generator that returns a unique scoped key value."""
        return "skid_form123"

    service._generate_unique_key_id = generate_key_id  # type: ignore[method-assign]

    key, secret = await service.create_key(
        CreateLeadIngestionKeyRequest(name="Website form API"),
        resource_type=PublicIngressResourceType.LEAD_FORM,
        resource_id=form_id,
        metadata={"form_name": "Website demo"},
    )

    assert key.public_key == "skid_form123"
    assert secret.startswith("cgtm_live_")
    assert key.key_type == PublicIngressKeyType.PRIVATE_API
    assert key.resource_type == PublicIngressResourceType.LEAD_FORM
    assert key.resource_id == form_id
    assert key.organisation_id == org_id
    assert key.key_metadata["form_name"] == "Website demo"


@pytest.mark.asyncio
async def test_auth_rejects_expired_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expired private keys should fail authorization with key_expired code."""

    async def load_key(
        self: PrivateLeadIngestionAuthService,
        key_id: str,
        **_kwargs,
    ) -> PublicIngressKey:
        """Return a deterministic expired key to drive the authorization rejection path."""
        org_id = uuid4()
        return PublicIngressKey(
            organisation_id=org_id,
            key_type=PublicIngressKeyType.PRIVATE_API,
            resource_type=PublicIngressResourceType.LEAD_FORM,
            resource_id=uuid4(),
            public_key=key_id,
            secret_hash="hash",
            status=PublicIngressKeyStatus.ACTIVE,
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )

    monkeypatch.setattr(PrivateLeadIngestionAuthService, "_load_key", load_key)

    service = PrivateLeadIngestionAuthService(RecordingAsyncSession())  # type: ignore[arg-type]

    with pytest.raises(PrivateLeadIngestionAuthError) as exc_info:
        await service.authenticate(
            key_id="skid_test",
            authorization="Bearer secret",
            client_ip="203.0.113.10",
            resource_type=PublicIngressResourceType.LEAD_FORM,
        )

    assert exc_info.value.code == "key_expired"


@pytest.mark.asyncio
async def test_private_ingestion_requires_idempotency_key() -> None:
    """Requests missing idempotency token should be rejected before any db write."""
    service = PrivateLeadIngestionService(
        org_session=RecordingAsyncSession(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
    )

    with pytest.raises(PrivateLeadIngestionServiceError) as exc_info:
        await service.ingest(
            payload=PrivateLeadIngestionRequest(email="buyer@example.com"),
            key_id="skid_test",
            idempotency_key=None,
        )

    assert exc_info.value.code == "missing_idempotency_key"


@pytest.mark.asyncio
async def test_private_ingestion_rejects_oversized_idempotency_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Header replay tokens should be bounded before they hit String(255) storage."""
    session = RecordingAsyncSession()
    service = PrivateLeadIngestionService(
        org_session=session,  # type: ignore[arg-type]
        organisation_id=uuid4(),
    )
    replay_lookup_called = False

    async def _find_existing(
        self: PrivateLeadIngestionService, *, key_id: str, idempotency_key: str
    ):
        nonlocal replay_lookup_called
        replay_lookup_called = True
        return None

    monkeypatch.setattr(PrivateLeadIngestionService, "_find_existing", _find_existing)

    with pytest.raises(PrivateLeadIngestionServiceError) as exc_info:
        await service.ingest(
            payload=PrivateLeadIngestionRequest(email="buyer@example.com"),
            key_id="skid_direct",
            idempotency_key="x" * 256,
        )

    assert exc_info.value.code == "invalid_idempotency_key"
    assert exc_info.value.status_code == 400
    assert replay_lookup_called is False
    assert session.added == []


@pytest.mark.asyncio
async def test_private_ingestion_rejects_oversized_key_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private key ids should be bounded before they hit String(128) storage."""
    session = RecordingAsyncSession()
    service = PrivateLeadIngestionService(
        org_session=session,  # type: ignore[arg-type]
        organisation_id=uuid4(),
    )
    replay_lookup_called = False

    async def _find_existing(
        self: PrivateLeadIngestionService, *, key_id: str, idempotency_key: str
    ):
        nonlocal replay_lookup_called
        replay_lookup_called = True
        return None

    monkeypatch.setattr(PrivateLeadIngestionService, "_find_existing", _find_existing)

    with pytest.raises(PrivateLeadIngestionServiceError) as exc_info:
        await service.ingest(
            payload=PrivateLeadIngestionRequest(email="buyer@example.com"),
            key_id="x" * 129,
            idempotency_key="lead-123",
        )

    assert exc_info.value.code == "invalid_key_id"
    assert exc_info.value.status_code == 400
    assert replay_lookup_called is False
    assert session.added == []


@pytest.mark.asyncio
async def test_private_ingestion_persists_idempotency_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct private ingestion stores replay keys in dedicated DB columns."""
    session = RecordingAsyncSession()
    org_id = uuid4()
    service = PrivateLeadIngestionService(
        org_session=session,  # type: ignore[arg-type]
        organisation_id=org_id,
    )

    async def _find_existing(
        self: PrivateLeadIngestionService, *, key_id: str, idempotency_key: str
    ):
        """Force find_existing to miss so new ingestion can proceed."""
        return None

    monkeypatch.setattr(PrivateLeadIngestionService, "_find_existing", _find_existing)

    lead, replayed = await service.ingest(
        payload=PrivateLeadIngestionRequest(email="Buyer@Example.com"),
        key_id="skid_direct",
        idempotency_key=" lead-123 ",
    )

    assert replayed is False
    assert lead in session.added
    assert lead.ingestion_key_id == "skid_direct"
    assert lead.idempotency_key == "lead-123"
    assert lead.properties["_copilotgtm_ingestion"]["idempotency_key"] == "lead-123"


@pytest.mark.asyncio
async def test_private_ingestion_replays_by_persisted_idempotency_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry lookup should use the persisted columns, not JSON metadata traversal."""
    session = RecordingAsyncSession()
    org_id = uuid4()
    existing_lead = object()
    service = PrivateLeadIngestionService(
        org_session=session,  # type: ignore[arg-type]
        organisation_id=org_id,
    )
    calls: list[tuple[str, str]] = []

    async def _find_existing(
        self: PrivateLeadIngestionService, *, key_id: str, idempotency_key: str
    ):
        calls.append((key_id, idempotency_key))
        return existing_lead

    monkeypatch.setattr(PrivateLeadIngestionService, "_find_existing", _find_existing)

    lead, replayed = await service.ingest(
        payload=PrivateLeadIngestionRequest(email="buyer@example.com"),
        key_id="skid_direct",
        idempotency_key="lead-123",
    )

    assert lead is existing_lead
    assert replayed is True
    assert calls == [("skid_direct", "lead-123")]
    assert session.added == []


@pytest.mark.asyncio
async def test_private_ingestion_unique_race_reloads_existing_lead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB unique conflict should rollback and replay the committed winner row."""
    existing_lead = object()
    session = RecordingAsyncSession(
        commit_error=IntegrityError("insert", {}, Exception("unique violation"))
    )
    service = PrivateLeadIngestionService(
        org_session=session,  # type: ignore[arg-type]
        organisation_id=uuid4(),
    )
    lookup_count = 0

    async def _find_existing(
        self: PrivateLeadIngestionService, *, key_id: str, idempotency_key: str
    ):
        nonlocal lookup_count
        lookup_count += 1
        return None if lookup_count == 1 else existing_lead

    monkeypatch.setattr(PrivateLeadIngestionService, "_find_existing", _find_existing)

    lead, replayed = await service.ingest(
        payload=PrivateLeadIngestionRequest(email="buyer@example.com"),
        key_id="skid_direct",
        idempotency_key="lead-123",
    )

    assert lead is existing_lead
    assert replayed is True
    assert session.rollbacks == 1
    assert lookup_count == 2


def test_private_ingestion_rejects_form_source_type() -> None:
    """FORM source-type should be rejected by private API ingestion validation."""
    service = PrivateLeadIngestionService(
        org_session=RecordingAsyncSession(),  # type: ignore[arg-type]
        organisation_id=uuid4(),
    )

    with pytest.raises(PrivateLeadIngestionServiceError) as exc_info:
        service._validate_payload(
            PrivateLeadIngestionRequest(
                source_type=CRMLeadSourceType.FORM,
                email="buyer@example.com",
            )
        )

    assert exc_info.value.code == "invalid_source_type"
