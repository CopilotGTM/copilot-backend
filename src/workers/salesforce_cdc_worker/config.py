"""Validated standard-library settings for the dedicated Salesforce CDC worker."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class SalesforceCdcWorkerSettings:
    """Bound one process to the approved seven-integration operating envelope."""

    max_integrations: int = 7
    discovery_seconds: int = 60
    stream_runtime_seconds: int = 2700
    lease_seconds: int = 120
    lease_renew_seconds: int = 30
    reconnect_max_seconds: int = 60
    shutdown_seconds: int = 90

    def __post_init__(self) -> None:
        """Reject capacity or timing values that make the lease contract unsafe."""
        if not 1 <= self.max_integrations <= 7:
            raise ValueError("max_integrations must be between 1 and 7")
        if any(
            value <= 0
            for value in (
                self.discovery_seconds,
                self.stream_runtime_seconds,
                self.reconnect_max_seconds,
                self.shutdown_seconds,
            )
        ):
            raise ValueError("worker timing values must be positive")
        if not 0 < self.lease_renew_seconds < self.lease_seconds:
            raise ValueError("lease_renew_seconds must be less than lease_seconds")

    @classmethod
    def from_env(cls) -> SalesforceCdcWorkerSettings:
        """Read optional deployment overrides without introducing a settings dependency."""
        return cls(
            max_integrations=int(os.getenv("SALESFORCE_CDC_MAX_INTEGRATIONS", "7")),
            discovery_seconds=int(os.getenv("SALESFORCE_CDC_DISCOVERY_SECONDS", "60")),
            stream_runtime_seconds=int(os.getenv("SALESFORCE_CDC_STREAM_RUNTIME_SECONDS", "2700")),
            lease_seconds=int(os.getenv("SALESFORCE_CDC_LEASE_SECONDS", "120")),
            lease_renew_seconds=int(os.getenv("SALESFORCE_CDC_LEASE_RENEW_SECONDS", "30")),
            reconnect_max_seconds=int(os.getenv("SALESFORCE_CDC_RECONNECT_MAX_SECONDS", "60")),
            shutdown_seconds=int(os.getenv("SALESFORCE_CDC_SHUTDOWN_SECONDS", "90")),
        )
