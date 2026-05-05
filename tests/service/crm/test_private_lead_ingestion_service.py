"""Service-level tests for private lead-ingestion key crypto and validation flow."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
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
from src.service.crm.access.lead_ingestion_key_service import LeadIngestionKeyService
from src.service.crm.access.private_lead_ingestion_auth_service import (
    PrivateLeadIngestionAuthError,
    PrivateLeadIngestionAuthService,
)
from src.service.crm.access.private_lead_ingestion_service import (
    PrivateLeadIngestionService,
    PrivateLeadIngestionServiceError,
)


class RecordingAsyncSession:
    """Minimal async session stub capturing commit/refresh behavior for service tests."""

    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0
        self.refreshed: list[object] = []

    def add(self, instance: object) -> None:
        self.added.append(instance)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, instance: object) -> None:
        self.refreshed.append(instance)

    async def close(self) -> None:
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
