"""Signed webhook credential, replay, schema, and direct-dispatch contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from src.domain.models.organisation_tables import AutomationEventDispatch, AutomationTrigger
from src.service.automation.errors import AutomationServiceError
from src.service.automation.events.contracts import AUTOMATION_EVENT_MAX_BYTES
from src.service.automation.triggers.webhook import (
    WEBHOOK_MAX_BODY_BYTES,
    WebhookIngressService,
    organisation_id_from_endpoint_token,
    provision_webhook_config,
    rotate_webhook_secret,
    sign_webhook,
)

ROOT_SECRET = "test-only-root-secret"


def public_config(*, additional_properties: bool = False):
    """Build the public config fixture used by the surrounding regression cases."""
    return {
        "payload_schema": {
            "type": "object",
            "properties": {"risk": {"type": "string", "maxLength": 20}},
            "required": ["risk"],
            "additionalProperties": additional_properties,
        }
    }


def binding(organisation_id, config):  # noqa: ANN001
    """Build the binding fixture used by the surrounding regression cases."""
    return AutomationTrigger(
        organisation_id=organisation_id,
        created_by_user_id=uuid4(),
        name="External signal",
        target_id=uuid4(),
        target_revision=1,
        target_snapshot={},
        trigger_key="integration.webhook.received",
        family="webhook",
        match_event_keys=["integration.webhook.received"],
        trigger_config=config,
        execution_principal_type="organisation_service",
        execution_principal_id=organisation_id,
        scope_type="organisation",
        audience_mode="none",
        config_hash="c" * 64,
        status="active",
    )


class Access:
    """Represent the Access test double used by the surrounding regression cases."""

    def __init__(self, trigger):  # noqa: ANN001
        self.trigger = trigger
        self.hashes = []
        self.dispatches = {}

    async def get_active_trigger(self, endpoint_token_hash):  # noqa: ANN001
        """Expose the get active trigger operation expected by the Access test double."""
        self.hashes.append(endpoint_token_hash)
        return self.trigger

    async def get_dispatch(self, idempotency_key):  # noqa: ANN001
        """Expose the get dispatch operation expected by the Access test double."""
        return self.dispatches.get(idempotency_key)


class Enqueuer:
    """Represent the Enqueuer test double used by the surrounding regression cases."""

    def __init__(self, access=None):  # noqa: ANN001
        self.access = access
        self.envelopes = []

    async def enqueue(self, envelope):  # noqa: ANN001
        """Build the enqueue fixture used by the surrounding regression cases."""
        self.envelopes.append(envelope)
        if self.access is None:
            return object()
        dispatch = self.access.dispatches.get(envelope.idempotency_key)
        if dispatch is None:
            dispatch = AutomationEventDispatch(
                event_id=envelope.event_id,
                organisation_id=envelope.organisation_id,
                event_key=envelope.event_key,
                route_mode="direct",
                direct_trigger_id=envelope.route.trigger_id,
                idempotency_key=envelope.idempotency_key,
                envelope=envelope.model_dump(mode="json"),
            )
            self.access.dispatches[envelope.idempotency_key] = dispatch
        return dispatch


def credentials(organisation_id, *, now):  # noqa: ANN001
    """Build the credentials fixture used by the surrounding regression cases."""
    return provision_webhook_config(
        public_config(),
        organisation_id=organisation_id,
        root_secret=ROOT_SECRET,
        now=now,
        random_bytes=lambda _size: b"r" * 32,
    )


def test_webhook_credentials_persist_only_hashes_and_derive_tenant() -> None:
    """Verify the contract that webhook credentials persist only hashes and derive tenant."""
    organisation_id = uuid4()
    config, issued = credentials(
        organisation_id,
        now=datetime(2026, 8, 5, 10, tzinfo=UTC),
    )

    assert organisation_id_from_endpoint_token(issued.endpoint_token) == organisation_id
    assert issued.endpoint_token not in json.dumps(config)
    assert issued.secret not in json.dumps(config)
    assert config["_webhook_endpoint_token_hash"]
    assert config["_webhook_secret_hash"]
    assert config["payload_schema"] == public_config()["payload_schema"]


@pytest.mark.asyncio
async def test_valid_webhook_dispatch_is_replay_stable_and_content_bounded() -> None:
    """Verify the contract that valid webhook dispatch is replay stable and content bounded."""
    organisation_id = uuid4()
    now = datetime(2026, 8, 5, 10, tzinfo=UTC)
    config, issued = credentials(organisation_id, now=now)
    trigger = binding(organisation_id, config)
    access = Access(trigger)
    enqueuer = Enqueuer(access)
    body = b'{"risk":"high"}'
    timestamp = str(int(now.timestamp()))
    signature = sign_webhook(issued.secret, timestamp, body)
    service = WebhookIngressService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        issued.endpoint_token,
        root_secret=ROOT_SECRET,
        access=access,
        event_enqueuer=enqueuer,
        clock=lambda: now,
    )

    first = await service.ingest(
        raw_body=body,
        timestamp=timestamp,
        signature=signature,
        dedupe_key="provider-event-1",
    )
    replay_timestamp = str(int((now + timedelta(seconds=1)).timestamp()))
    replay = await service.ingest(
        raw_body=body,
        timestamp=replay_timestamp,
        signature=sign_webhook(issued.secret, replay_timestamp, body),
        dedupe_key="provider-event-1",
    )

    assert first.envelope == replay.envelope
    assert first.envelope.record == {"risk": "high"}
    assert first.envelope.route.trigger_id == trigger.trigger_id
    assert first.envelope.route.config_hash == trigger.config_hash
    assert first.envelope.attributes == {"dedupe_key": "provider-event-1"}
    assert first.envelope.actor.type == "external"
    assert len(enqueuer.envelopes) == 2

    changed_body = b'{"risk":"low"}'
    with pytest.raises(AutomationServiceError, match="different payload"):
        await service.ingest(
            raw_body=changed_body,
            timestamp=timestamp,
            signature=sign_webhook(issued.secret, timestamp, changed_body),
            dedupe_key="provider-event-1",
        )

    with pytest.raises(AutomationServiceError, match="too large"):
        await service.ingest(
            raw_body=b"x" * (WEBHOOK_MAX_BODY_BYTES + 1),
            timestamp=timestamp,
            signature=signature,
        )


@pytest.mark.asyncio
async def test_payload_hash_replays_exact_request_but_not_a_later_delivery() -> None:
    """Scope hash-only idempotency to the signed request timestamp."""
    organisation_id = uuid4()
    now = datetime(2026, 8, 5, 10, tzinfo=UTC)
    config, issued = credentials(organisation_id, now=now)
    access = Access(binding(organisation_id, config))
    enqueuer = Enqueuer(access)
    body = b'{"risk":"high"}'
    timestamp = str(int(now.timestamp()))
    service = WebhookIngressService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        issued.endpoint_token,
        root_secret=ROOT_SECRET,
        access=access,
        event_enqueuer=enqueuer,
        clock=lambda: now,
    )

    first = await service.ingest(
        raw_body=body,
        timestamp=timestamp,
        signature=sign_webhook(issued.secret, timestamp, body),
    )
    exact_replay = await service.ingest(
        raw_body=body,
        timestamp=timestamp,
        signature=sign_webhook(issued.secret, timestamp, body),
    )

    later = now + timedelta(minutes=6)
    later_timestamp = str(int(later.timestamp()))
    later_delivery = await WebhookIngressService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        issued.endpoint_token,
        root_secret=ROOT_SECRET,
        access=access,
        event_enqueuer=enqueuer,
        clock=lambda: later,
    ).ingest(
        raw_body=body,
        timestamp=later_timestamp,
        signature=sign_webhook(issued.secret, later_timestamp, body),
    )

    assert first.envelope.event_id == exact_replay.envelope.event_id
    assert later_delivery.envelope.event_id != first.envelope.event_id
    assert len(access.dispatches) == 2


@pytest.mark.asyncio
async def test_maximum_webhook_body_fits_inside_the_canonical_envelope() -> None:
    """Reserve enough ingress headroom for required envelope metadata."""
    organisation_id = uuid4()
    now = datetime(2026, 8, 5, 10, tzinfo=UTC)
    config, issued = provision_webhook_config(
        {
            "payload_schema": {
                "type": "object",
                "properties": {"blob": {"type": "string"}},
                "required": ["blob"],
                "additionalProperties": False,
            }
        },
        organisation_id=organisation_id,
        root_secret=ROOT_SECRET,
        now=now,
        random_bytes=lambda _size: b"u" * 32,
    )
    prefix, suffix = b'{"blob":"', b'"}'
    body = prefix + b"x" * (WEBHOOK_MAX_BODY_BYTES - len(prefix) - len(suffix)) + suffix
    timestamp = str(int(now.timestamp()))

    result = await WebhookIngressService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        issued.endpoint_token,
        root_secret=ROOT_SECRET,
        access=Access(binding(organisation_id, config)),
        event_enqueuer=Enqueuer(),
        clock=lambda: now,
    ).ingest(
        raw_body=body,
        timestamp=timestamp,
        signature=sign_webhook(issued.secret, timestamp, body),
    )

    assert len(body) == WEBHOOK_MAX_BODY_BYTES
    assert len(result.envelope.model_dump_json().encode("utf-8")) <= AUTOMATION_EVENT_MAX_BYTES


@pytest.mark.asyncio
async def test_root_rotation_keeps_existing_webhooks_verifiable() -> None:
    """Verify the contract that root rotation keeps existing webhooks verifiable."""
    organisation_id = uuid4()
    now = datetime(2026, 8, 5, 10, tzinfo=UTC)
    config, issued = credentials(organisation_id, now=now)
    access = Access(binding(organisation_id, config))
    body = b'{"risk":"high"}'
    timestamp = str(int(now.timestamp()))
    service = WebhookIngressService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        issued.endpoint_token,
        root_secret="rotated-root-secret",
        previous_root_secrets=(ROOT_SECRET,),
        access=access,
        event_enqueuer=Enqueuer(access),
        clock=lambda: now,
    )

    result = await service.ingest(
        raw_body=body,
        timestamp=timestamp,
        signature=sign_webhook(issued.secret, timestamp, body),
        dedupe_key="provider-event-after-root-rotation",
    )

    assert result.envelope.record == {"risk": "high"}


@pytest.mark.asyncio
async def test_webhook_rejects_signature_replay_schema_and_server_owned_fields() -> None:
    """Verify the contract that webhook rejects signature replay schema and server owned fields."""
    organisation_id = uuid4()
    now = datetime(2026, 8, 5, 10, tzinfo=UTC)
    config, issued = provision_webhook_config(
        public_config(additional_properties=True),
        organisation_id=organisation_id,
        root_secret=ROOT_SECRET,
        now=now,
        random_bytes=lambda _size: b"s" * 32,
    )
    service = WebhookIngressService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        issued.endpoint_token,
        root_secret=ROOT_SECRET,
        access=Access(binding(organisation_id, config)),
        event_enqueuer=Enqueuer(),
        clock=lambda: now,
    )

    with pytest.raises(AutomationServiceError, match="signature"):
        await service.ingest(
            raw_body=b'{"risk":"high"}',
            timestamp=str(int(now.timestamp())),
            signature="sha256=wrong",
        )

    stale_timestamp = str(int((now - timedelta(minutes=6)).timestamp()))
    with pytest.raises(AutomationServiceError, match="timestamp"):
        await service.ingest(
            raw_body=b'{"risk":"high"}',
            timestamp=stale_timestamp,
            signature=sign_webhook(issued.secret, stale_timestamp, b'{"risk":"high"}'),
        )

    for body, message in (
        (b'{"risk":12}', "schema"),
        (b'{"risk":"high","organisation_id":"foreign"}', "server-owned"),
    ):
        timestamp = str(int(now.timestamp()))
        with pytest.raises(AutomationServiceError, match=message):
            await service.ingest(
                raw_body=body,
                timestamp=timestamp,
                signature=sign_webhook(issued.secret, timestamp, body),
            )


@pytest.mark.asyncio
async def test_webhook_revalidates_stored_schema_without_resolving_external_refs() -> None:
    """Verify the contract that webhook revalidates stored schema without resolving external refs."""
    organisation_id = uuid4()
    now = datetime(2026, 8, 5, 10, tzinfo=UTC)
    config, issued = provision_webhook_config(
        {"payload_schema": {"$ref": "other.json"}},
        organisation_id=organisation_id,
        root_secret=ROOT_SECRET,
        now=now,
        random_bytes=lambda _size: b"t" * 32,
    )
    body = b'{"risk":"high"}'
    timestamp = str(int(now.timestamp()))
    service = WebhookIngressService(
        object(),  # type: ignore[arg-type]
        organisation_id,
        issued.endpoint_token,
        root_secret=ROOT_SECRET,
        access=Access(binding(organisation_id, config)),
        event_enqueuer=Enqueuer(),
        clock=lambda: now,
    )

    with pytest.raises(AutomationServiceError, match="configured schema"):
        await service.ingest(
            raw_body=body,
            timestamp=timestamp,
            signature=sign_webhook(issued.secret, timestamp, body),
        )


@pytest.mark.asyncio
async def test_rotated_webhook_secret_has_a_bounded_grace_window() -> None:
    """Verify the contract that rotated webhook secret has a bounded grace window."""
    organisation_id = uuid4()
    created_at = datetime(2026, 8, 5, 10, tzinfo=UTC)
    config, issued = credentials(organisation_id, now=created_at)
    rotated_at = created_at + timedelta(minutes=1)
    rotated_config, new_secret = rotate_webhook_secret(
        config,
        organisation_id=organisation_id,
        root_secret=ROOT_SECRET,
        now=rotated_at,
        grace_period=timedelta(minutes=10),
    )
    trigger = binding(organisation_id, rotated_config)
    body = b'{"risk":"high"}'

    for clock, secret, accepted in (
        (rotated_at, issued.secret, True),
        (rotated_at, new_secret, True),
        (rotated_at + timedelta(minutes=11), issued.secret, False),
    ):
        timestamp = str(int(clock.timestamp()))
        invocation = WebhookIngressService(
            object(),  # type: ignore[arg-type]
            organisation_id,
            issued.endpoint_token,
            root_secret=ROOT_SECRET,
            access=Access(trigger),
            event_enqueuer=Enqueuer(),
            clock=lambda clock=clock: clock,
        ).ingest(
            raw_body=body,
            timestamp=timestamp,
            signature=sign_webhook(secret, timestamp, body),
        )
        if accepted:
            await invocation
        else:
            with pytest.raises(AutomationServiceError, match="signature"):
                await invocation
