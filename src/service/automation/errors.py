"""Stable expected errors returned by automation services."""


class AutomationServiceError(Exception):
    """Describe a safe automation-domain failure for HTTP and internal callers."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
