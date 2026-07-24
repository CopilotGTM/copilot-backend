"""Stable domain errors returned by user-agent services."""


class AgentServiceError(Exception):
    """Describe an expected agent-domain failure without leaking internal details."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
