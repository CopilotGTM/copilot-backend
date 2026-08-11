import pytest
from src.workers.salesforce_cdc_worker.config import SalesforceCdcWorkerSettings


def test_settings_accept_at_most_seven_integrations() -> None:
    assert SalesforceCdcWorkerSettings(max_integrations=7).max_integrations == 7
    with pytest.raises(ValueError, match="between 1 and 7"):
        SalesforceCdcWorkerSettings(max_integrations=8)


def test_settings_read_all_runtime_contract_values(monkeypatch) -> None:
    monkeypatch.setenv("SALESFORCE_CDC_STREAM_RUNTIME_SECONDS", "2400")
    monkeypatch.setenv("SALESFORCE_CDC_LEASE_SECONDS", "150")
    monkeypatch.setenv("SALESFORCE_CDC_LEASE_RENEW_SECONDS", "25")

    settings = SalesforceCdcWorkerSettings.from_env()

    assert settings.stream_runtime_seconds == 2400
    assert settings.lease_seconds == 150
    assert settings.lease_renew_seconds == 25
