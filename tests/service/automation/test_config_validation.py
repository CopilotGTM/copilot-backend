"""Shared JSON trust-boundary tests for agent and trigger configuration."""

from __future__ import annotations

import pytest
from src.service.automation.config_validation import (
    contains_credentials,
    validate_json_document,
)


def test_credential_detection_covers_provider_prefixes_without_blocking_model_tokens() -> None:
    assert contains_credentials({"provider": {"openai_api_key": "secret"}})
    assert contains_credentials({"provider": {"salesforce_access_token": "secret"}})
    assert not contains_credentials({"stop_token": "END", "max_tokens": 2_000})


def test_json_validation_rejects_non_json_and_oversized_documents() -> None:
    with pytest.raises(ValueError, match="trigger_config must contain valid JSON values"):
        validate_json_document("trigger_config", {"invalid": {1, 2}}, max_bytes=1_000)

    with pytest.raises(ValueError, match="trigger_config exceeds 32 bytes"):
        validate_json_document("trigger_config", {"notes": "x" * 40}, max_bytes=32)

    validate_json_document("trigger_config", {"temperature": 0.2}, max_bytes=1_000)
