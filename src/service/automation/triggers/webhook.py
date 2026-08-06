"""Derived webhook credentials and signed replay-safe direct ingestion."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing.exceptions import Unresolvable
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables import (
    AutomationEventDispatch,
    AutomationTrigger,
    AutomationTriggerFamily,
    AutomationTriggerStatus,
)
from src.service.automation.errors import AutomationServiceError
from src.service.automation.events.access import AutomationEventDispatchAccess
from src.service.automation.events.contracts import AutomationEventEnvelope
from src.service.automation.events.service import AutomationEventDispatchService

# Reserve 4 KiB for server-owned envelope metadata under the 64 KiB durable
# event limit; otherwise a valid near-limit request can fail after authentication.
WEBHOOK_MAX_BODY_BYTES = 60 * 1024
WEBHOOK_REPLAY_WINDOW = timedelta(minutes=5)
WEBHOOK_SECRET_GRACE_PERIOD = timedelta(hours=24)
WEBHOOK_MAX_TOP_LEVEL_FIELDS = 64

_ENDPOINT_HASH = "_webhook_endpoint_token_hash"
_SECRET_HASH = "_webhook_secret_hash"
_SECRET_CREATED_AT = "_webhook_secret_created_at"
_ROOT_KEY_ID = "_webhook_root_key_id"
_PREVIOUS_SECRET_HASH = "_webhook_previous_secret_hash"
_PREVIOUS_SECRET_CREATED_AT = "_webhook_previous_secret_created_at"
_PREVIOUS_SECRET_EXPIRES_AT = "_webhook_previous_secret_expires_at"
_PREVIOUS_ROOT_KEY_ID = "_webhook_previous_root_key_id"
_INTERNAL_CONFIG_KEYS = frozenset(
    {
        _ENDPOINT_HASH,
        _SECRET_HASH,
        _SECRET_CREATED_AT,
        _ROOT_KEY_ID,
        _PREVIOUS_SECRET_HASH,
        _PREVIOUS_SECRET_CREATED_AT,
        _PREVIOUS_SECRET_EXPIRES_AT,
        _PREVIOUS_ROOT_KEY_ID,
    }
)
_RESERVED_PAYLOAD_FIELDS = frozenset(
    {
        "organisation_id",
        "organization_id",
        "event_id",
        "config_hash",
        "trigger_id",
    }
)


@dataclass(frozen=True)
class WebhookIssuedCredentials:
    """Plaintext values returned once while only derivation evidence persists."""

    endpoint_token: str
    secret: str


@dataclass(frozen=True)
class WebhookIngressItem:
    """Validated direct envelope and its durable dispatch row."""

    envelope: AutomationEventEnvelope
    dispatch: AutomationEventDispatch


def provision_webhook_config(
    public_config: Mapping[str, Any],
    *,
    organisation_id: UUID,
    root_secret: str,
    now: datetime,
    random_bytes: Callable[[int], bytes] = secrets.token_bytes,
) -> tuple[dict[str, Any], WebhookIssuedCredentials]:
    """Add hashes and derivation time while returning plaintext credentials once."""

    endpoint_token = _encode_token(organisation_id.bytes + random_bytes(32))
    endpoint_hash = _sha256(endpoint_token)
    created_at = _utc_iso(now)
    secret = _derive_secret(root_secret, organisation_id, endpoint_hash, created_at)
    config = dict(public_config)
    config.update(
        {
            _ENDPOINT_HASH: endpoint_hash,
            _SECRET_HASH: _sha256(secret),
            _SECRET_CREATED_AT: created_at,
            _ROOT_KEY_ID: _sha256(root_secret),
        }
    )
    return config, WebhookIssuedCredentials(endpoint_token, secret)


def rotate_webhook_secret(
    config: Mapping[str, Any],
    *,
    organisation_id: UUID,
    root_secret: str,
    now: datetime,
    grace_period: timedelta = WEBHOOK_SECRET_GRACE_PERIOD,
) -> tuple[dict[str, Any], str]:
    """Derive a new signing secret and retain the old derivation for a short grace."""

    endpoint_hash = _required_text(config, _ENDPOINT_HASH)
    current_hash = _required_text(config, _SECRET_HASH)
    current_created_at = _required_text(config, _SECRET_CREATED_AT)
    created_at = _utc_iso(now)
    if created_at == current_created_at:
        created_at = _utc_iso(now + timedelta(microseconds=1))
    secret = _derive_secret(root_secret, organisation_id, endpoint_hash, created_at)
    updated = dict(config)
    updated.update(
        {
            _SECRET_HASH: _sha256(secret),
            _SECRET_CREATED_AT: created_at,
            _ROOT_KEY_ID: _sha256(root_secret),
            _PREVIOUS_SECRET_HASH: current_hash,
            _PREVIOUS_SECRET_CREATED_AT: current_created_at,
            _PREVIOUS_SECRET_EXPIRES_AT: _utc_iso(now + grace_period),
            _PREVIOUS_ROOT_KEY_ID: _required_text(config, _ROOT_KEY_ID),
        }
    )
    return updated, secret


def public_webhook_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Remove credential derivation evidence from owner-facing responses."""

    return {key: value for key, value in config.items() if key not in _INTERNAL_CONFIG_KEYS}


def webhook_config_is_provisioned(config: Mapping[str, Any]) -> bool:
    """Return whether a stored binding has every current credential projection."""

    return all(
        isinstance(config.get(key), str) and config[key]
        for key in (
            _ENDPOINT_HASH,
            _SECRET_HASH,
            _SECRET_CREATED_AT,
            _ROOT_KEY_ID,
        )
    )


def validate_webhook_payload_schema(schema: Any) -> dict[str, Any]:
    """Accept local JSON Schema references without creating a network boundary."""

    if not isinstance(schema, dict):
        raise ValueError("payload_schema must be a JSON Schema object")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError("payload_schema must be valid JSON Schema") from exc
    pending: list[Any] = [schema]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    key in {"$ref", "$dynamicRef"}
                    and isinstance(value, str)
                    and not value.startswith("#")
                ):
                    raise ValueError("payload_schema cannot contain external references")
                pending.append(value)
        elif isinstance(node, list):
            pending.extend(node)
    return schema


def retain_webhook_credentials(
    public_config: Mapping[str, Any],
    stored_config: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep server-owned credential state across an owner-visible config replace."""

    if not webhook_config_is_provisioned(stored_config):
        raise ValueError("stored webhook credentials are unavailable")
    merged = dict(public_config)
    merged.update(
        {key: stored_config[key] for key in _INTERNAL_CONFIG_KEYS if key in stored_config}
    )
    return merged


def organisation_id_from_endpoint_token(endpoint_token: str) -> UUID:
    """Derive the tenant boundary from the unguessable route token itself."""

    try:
        raw = _decode_token(endpoint_token)
    except (ValueError, TypeError) as exc:
        raise ValueError("Invalid automation webhook endpoint") from exc
    if len(raw) != 48:
        raise ValueError("Invalid automation webhook endpoint")
    return UUID(bytes=raw[:16])


def sign_webhook(secret: str, timestamp: str, raw_body: bytes) -> str:
    """Return the documented HMAC-SHA256 signature over timestamp and raw body."""

    digest = hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("ascii") + b"." + raw_body,
        hashlib.sha256,
    ).hexdigest()
    return f"sha256={digest}"


class WebhookTriggerAccessProtocol(Protocol):
    """Endpoint-hash lookup inside the tenant derived from the route token."""

    async def get_active_trigger(self, endpoint_token_hash: str) -> AutomationTrigger | None:
        """Load active trigger within the organisation boundary."""
        ...

    async def get_dispatch(self, idempotency_key: str) -> AutomationEventDispatch | None:
        """Load dispatch within the organisation boundary."""
        ...


class WebhookEventEnqueuer(Protocol):
    """Transaction-local canonical event ingestion."""

    async def enqueue(self, envelope: AutomationEventEnvelope) -> AutomationEventDispatch:
        """Queue the current Webhook Event Enqueuer work while preserving its tenant and idempotency boundaries."""
        ...


class WebhookTriggerAccess:
    """Resolve one active webhook binding without scanning unrelated triggers."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        *,
        event_access: AutomationEventDispatchAccess | None = None,
    ) -> None:
        self.session = session
        self.organisation_id = organisation_id
        self.event_access = event_access or AutomationEventDispatchAccess(
            session,
            organisation_id,
        )

    async def get_active_trigger(self, endpoint_token_hash: str) -> AutomationTrigger | None:
        """Load active trigger within the organisation boundary."""
        columns = cast(Any, AutomationTrigger).__table__.c
        # copilotgtm: Active webhook bindings are a small per-tenant set; add a
        # partial JSON-expression index if tenant webhook volume makes this scan material.
        statement = select(AutomationTrigger).where(
            columns.organisation_id == self.organisation_id,
            columns.family == AutomationTriggerFamily.WEBHOOK.value,
            columns.status == AutomationTriggerStatus.ACTIVE.value,
            columns.trigger_config[_ENDPOINT_HASH].astext == endpoint_token_hash,
        )
        return (await self.session.execute(statement)).scalars().first()

    async def get_dispatch(self, idempotency_key: str) -> AutomationEventDispatch | None:
        """Load dispatch within the organisation boundary."""
        return await self.event_access.get_by_idempotency_key(idempotency_key)


class WebhookIngressService:
    """Authenticate, validate, and enqueue one external webhook delivery."""

    def __init__(
        self,
        session: AsyncSession,
        organisation_id: UUID,
        endpoint_token: str,
        *,
        root_secret: str,
        previous_root_secrets: tuple[str, ...] = (),
        access: WebhookTriggerAccessProtocol | None = None,
        event_enqueuer: WebhookEventEnqueuer | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        try:
            token_organisation_id = organisation_id_from_endpoint_token(endpoint_token)
        except ValueError as exc:
            raise AutomationServiceError(
                "Automation webhook endpoint not found", status_code=404
            ) from exc
        if token_organisation_id != organisation_id:
            raise AutomationServiceError("Automation webhook endpoint not found", status_code=404)
        if not root_secret:
            raise AutomationServiceError(
                "Automation webhook verification is unavailable",
                status_code=503,
            )
        self.session = session
        self.organisation_id = organisation_id
        self.endpoint_token = endpoint_token
        self.root_secrets = {
            _sha256(secret): secret for secret in (root_secret, *previous_root_secrets) if secret
        }
        self.access = access or WebhookTriggerAccess(session, organisation_id)
        self.event_enqueuer = event_enqueuer or AutomationEventDispatchService(
            session,
            organisation_id,
        )
        self.clock = clock

    async def ingest(
        self,
        *,
        raw_body: bytes,
        timestamp: str,
        signature: str,
        dedupe_key: str | None = None,
    ) -> WebhookIngressItem:
        """Verify the exact bytes before parsing and persist a direct event."""

        if len(raw_body) > WEBHOOK_MAX_BODY_BYTES:
            raise AutomationServiceError("Automation webhook body is too large", status_code=413)
        endpoint_hash = _sha256(self.endpoint_token)
        trigger = await self.access.get_active_trigger(endpoint_hash)
        if trigger is None:
            raise AutomationServiceError("Automation webhook endpoint not found", status_code=404)
        event_time = self._verify_request(trigger, timestamp, signature, raw_body)
        payload = self._payload(trigger, raw_body)
        dedupe_key = self._dedupe_key(payload, dedupe_key, event_time)
        idempotency_key = f"webhook:{trigger.trigger_id}:{dedupe_key}"
        existing = await self.access.get_dispatch(idempotency_key)
        if existing is not None:
            envelope = self._replayed_envelope(
                existing,
                trigger=trigger,
                payload=payload,
                dedupe_key=dedupe_key,
            )
            return WebhookIngressItem(
                envelope=envelope,
                dispatch=await self.event_enqueuer.enqueue(envelope),
            )
        envelope = AutomationEventEnvelope.model_validate(
            {
                "event_id": uuid5(
                    NAMESPACE_URL,
                    f"automation:{self.organisation_id}:{idempotency_key}",
                ),
                "organisation_id": self.organisation_id,
                "event_key": "integration.webhook.received",
                "route": {
                    "mode": "direct",
                    "trigger_id": trigger.trigger_id,
                    "config_hash": trigger.config_hash,
                },
                "occurred_at": event_time,
                "source": {
                    "domain": "integration",
                    "system": "external_webhook",
                    "operation": "webhook",
                    "mode": "system",
                    "source_event_id": dedupe_key,
                },
                "actor": {"type": "external", "id": dedupe_key},
                "subject": {},
                "record": payload,
                "attributes": {"dedupe_key": dedupe_key},
                "correlation_id": f"webhook:{trigger.trigger_id}:{dedupe_key}",
                "idempotency_key": idempotency_key,
            }
        )
        return WebhookIngressItem(
            envelope=envelope,
            dispatch=await self.event_enqueuer.enqueue(envelope),
        )

    def _verify_request(
        self,
        trigger: AutomationTrigger,
        timestamp: str,
        signature: str,
        raw_body: bytes,
    ) -> datetime:
        try:
            epoch = int(timestamp)
            event_time = datetime.fromtimestamp(epoch, UTC)
        except (ValueError, OverflowError, OSError) as exc:
            raise AutomationServiceError("Invalid automation webhook timestamp") from exc
        now = self.clock().astimezone(UTC)
        if abs(now - event_time) > WEBHOOK_REPLAY_WINDOW:
            raise AutomationServiceError(
                "Automation webhook timestamp is outside the replay window"
            )
        if (
            len(signature) != 71
            or not signature.startswith("sha256=")
            or any(character not in "0123456789abcdef" for character in signature[7:])
        ):
            raise AutomationServiceError("Invalid automation webhook signature", status_code=401)

        config = trigger.trigger_config
        endpoint_hash = _required_text(config, _ENDPOINT_HASH)
        candidates = [
            self._secret(
                endpoint_hash,
                _required_text(config, _SECRET_CREATED_AT),
                _required_text(config, _SECRET_HASH),
                _required_text(config, _ROOT_KEY_ID),
            )
        ]
        previous_expires = config.get(_PREVIOUS_SECRET_EXPIRES_AT)
        if isinstance(previous_expires, str) and now <= _parse_utc(previous_expires):
            candidates.append(
                self._secret(
                    endpoint_hash,
                    _required_text(config, _PREVIOUS_SECRET_CREATED_AT),
                    _required_text(config, _PREVIOUS_SECRET_HASH),
                    _required_text(config, _PREVIOUS_ROOT_KEY_ID),
                )
            )
        if not any(
            hmac.compare_digest(sign_webhook(secret, timestamp, raw_body), signature)
            for secret in candidates
        ):
            raise AutomationServiceError("Invalid automation webhook signature", status_code=401)
        return event_time

    def _replayed_envelope(
        self,
        dispatch: AutomationEventDispatch,
        *,
        trigger: AutomationTrigger,
        payload: dict[str, Any],
        dedupe_key: str,
    ) -> AutomationEventEnvelope:
        try:
            envelope = AutomationEventEnvelope.model_validate(dispatch.envelope)
        except ValueError as exc:
            raise AutomationServiceError(
                "Stored automation webhook envelope is invalid",
                status_code=409,
            ) from exc
        if (
            envelope.organisation_id != self.organisation_id
            or envelope.event_key != "integration.webhook.received"
            or envelope.route.trigger_id != trigger.trigger_id
            or envelope.record != payload
            or envelope.attributes.get("dedupe_key") != dedupe_key
        ):
            raise AutomationServiceError(
                "Automation webhook delivery ID was reused for a different payload",
                status_code=409,
            )
        return envelope

    def _secret(
        self,
        endpoint_hash: str,
        created_at: str,
        expected_hash: str,
        root_key_id: str,
    ) -> str:
        root_secret = self.root_secrets.get(root_key_id)
        if root_secret is None:
            raise AutomationServiceError(
                "Automation webhook verification is unavailable",
                status_code=503,
            )
        secret = _derive_secret(
            root_secret,
            self.organisation_id,
            endpoint_hash,
            created_at,
        )
        if not hmac.compare_digest(_sha256(secret), expected_hash):
            raise AutomationServiceError(
                "Automation webhook verification is unavailable",
                status_code=503,
            )
        return secret

    @staticmethod
    def _payload(trigger: AutomationTrigger, raw_body: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(
                raw_body.decode("utf-8"),
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise AutomationServiceError("Automation webhook body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise AutomationServiceError("Automation webhook body must be a JSON object")
        if len(payload) > WEBHOOK_MAX_TOP_LEVEL_FIELDS:
            raise AutomationServiceError("Automation webhook body has too many fields")
        if set(payload) & _RESERVED_PAYLOAD_FIELDS:
            raise AutomationServiceError("Automation webhook body contains server-owned fields")
        try:
            schema = validate_webhook_payload_schema(trigger.trigger_config.get("payload_schema"))
            error = next(Draft202012Validator(schema).iter_errors(payload), None)
        except (Unresolvable, ValueError) as exc:
            raise AutomationServiceError(
                "Automation webhook configured schema is invalid",
                status_code=503,
            ) from exc
        if error is not None:
            raise AutomationServiceError(
                "Automation webhook payload does not match the configured schema",
                status_code=422,
            )
        return cast(dict[str, Any], payload)

    @staticmethod
    def _dedupe_key(
        payload: dict[str, Any],
        value: str | None,
        event_time: datetime,
    ) -> str:
        if value is not None:
            value = value.strip()
            if not value or len(value) > 200:
                raise AutomationServiceError(
                    "Automation webhook delivery ID must contain at most 200 characters"
                )
            return value
        canonical = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        # copilotgtm: Without a provider delivery ID, identical payloads signed in
        # the same second coalesce. Senders needing finer identity must supply one.
        signed_timestamp = str(int(event_time.timestamp())).encode("ascii")
        return hashlib.sha256(signed_timestamp + b"." + canonical).hexdigest()


def _derive_secret(
    root_secret: str,
    organisation_id: UUID,
    endpoint_hash: str,
    created_at: str,
) -> str:
    if not root_secret:
        raise ValueError("webhook root secret is required")
    material = (
        f"copilotgtm:automation-webhook:v1:{organisation_id}:{endpoint_hash}:{created_at}"
    ).encode()
    digest = hmac.new(root_secret.encode("utf-8"), material, hashlib.sha256).digest()
    return f"whsec_{_encode_token(digest)}"


def _required_text(config: Mapping[str, Any], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value:
        raise AutomationServiceError(
            "Automation webhook verification is unavailable",
            status_code=503,
        )
    return value


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("webhook credential timestamps must include timezone")
    return value.astimezone(UTC).isoformat()


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AutomationServiceError(
            "Automation webhook verification is unavailable",
            status_code=503,
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AutomationServiceError(
            "Automation webhook verification is unavailable",
            status_code=503,
        )
    return parsed.astimezone(UTC)


def _encode_token(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_token(value: str) -> bytes:
    if not value or len(value) > 128:
        raise ValueError("invalid token")
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


__all__ = [
    "WEBHOOK_MAX_BODY_BYTES",
    "WEBHOOK_REPLAY_WINDOW",
    "WEBHOOK_SECRET_GRACE_PERIOD",
    "WebhookIngressItem",
    "WebhookIngressService",
    "WebhookIssuedCredentials",
    "WebhookTriggerAccess",
    "organisation_id_from_endpoint_token",
    "provision_webhook_config",
    "public_webhook_config",
    "retain_webhook_credentials",
    "rotate_webhook_secret",
    "sign_webhook",
    "validate_webhook_payload_schema",
    "webhook_config_is_provisioned",
]
