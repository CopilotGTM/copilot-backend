from __future__ import annotations

from types import SimpleNamespace

import grpc
from grpc import aio
from src.workers.integrations_worker.providers.crm.salesforce.pubsub_client import (
    _next_fetch_request_count,
    _replay_preset_for,
)


def test_first_salesforce_pubsub_subscription_starts_from_latest_event() -> None:
    pb2 = SimpleNamespace(
        ReplayPreset=SimpleNamespace(LATEST="latest", EARLIEST="earliest", CUSTOM="custom")
    )

    assert _replay_preset_for(None, pb2) == "latest"


def test_salesforce_pubsub_subscription_uses_custom_replay_when_cursor_exists() -> None:
    pb2 = SimpleNamespace(
        ReplayPreset=SimpleNamespace(LATEST="latest", EARLIEST="earliest", CUSTOM="custom")
    )

    assert _replay_preset_for(b"\x00\x01", pb2) == "custom"


def test_salesforce_pubsub_subscription_can_start_from_earliest_event() -> None:
    pb2 = SimpleNamespace(
        ReplayPreset=SimpleNamespace(LATEST="latest", EARLIEST="earliest", CUSTOM="custom")
    )

    assert _replay_preset_for(None, pb2, start_from_earliest=True) == "earliest"


def test_salesforce_pubsub_cursor_overrides_earliest_preset() -> None:
    pb2 = SimpleNamespace(
        ReplayPreset=SimpleNamespace(LATEST="latest", EARLIEST="earliest", CUSTOM="custom")
    )

    assert (
        _replay_preset_for(
            b"\x00\x01",
            pb2,
            start_from_earliest=True,
        )
        == "custom"
    )


def test_salesforce_pubsub_tops_up_only_available_fetch_capacity() -> None:
    assert _next_fetch_request_count(pending_num_requested=7, num_requested=10) == 3
    assert _next_fetch_request_count(pending_num_requested=0, num_requested=10) == 10
    assert _next_fetch_request_count(pending_num_requested=10, num_requested=10) == 0
    assert _next_fetch_request_count(pending_num_requested=15, num_requested=10) == 0


def test_salesforce_pubsub_uses_supported_grpc_ssl_credentials_factory() -> None:
    assert callable(grpc.ssl_channel_credentials)
    assert hasattr(aio, "secure_channel")
    assert not hasattr(aio, "ssl_channel_credentials")
