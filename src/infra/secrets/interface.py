"""Interface / protocol definitions for the secrets subsystem.

Rationale
---------
Keeping explicit, minimal protocols lets the rest of the code depend only on
behaviour (duck typing) rather than concrete Azure SDK classes. This enables:

1. Easy swapping of the backend (e.g. environment variables, HashiCorp Vault,
   AWS Secrets Manager) without touching callers.
2. Simple fake providers for unit tests (deterministic & in‑memory).
3. Independent evolution of caching strategy.

The design uses two layers:
* SecretProvider  - talks to the *authoritative* secret store (network / IO)
* SecretCache      - in‑memory optimisation (TTL + size bound)

The manager (`manager.py`) composes these.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol


class SecretNotFound(Exception):
    """Raised when the provider determines a secret truly does not exist.

    Distinguish from transient errors so callers can implement different
    strategies (fail fast vs. retry/backoff)."""

    def __init__(self, name: str):
        super().__init__(f"Secret '{name}' not found")
        self.name = name


class TransientSecretError(Exception):
    """Raised when a temporary failure occurred (network, throttling, etc.)."""

    pass


class SecretProvider(Protocol):
    """Abstract provider contract.

    Implementations SHOULD:
    - Be idempotent for `get` calls.
    - Map backend specific errors to SecretNotFound / TransientSecretError.
    - Avoid returning empty string for non-existent; use None or raise.
    """

    async def get(self, name: str) -> str | None: ...  # noqa: D401 (protocol stub)
    async def get_many(self, names: Iterable[str]) -> dict[str, str | None]: ...


class SecretCache(Protocol):
    """Cache contract (sync – lightweight & in memory)."""

    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str): ...
    def invalidate(self, name: str): ...
    def clear(self): ...
