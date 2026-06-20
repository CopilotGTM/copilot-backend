from __future__ import annotations


class StakeholdersServiceError(Exception):
    """Error raised by participant-backed stakeholder services."""

    def __init__(
        self,
        message: str,
        status_code: int = 500,
        data: dict | None = None,
    ):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)
