"""Shared contracts for CRM lead services."""

from __future__ import annotations


class LeadServiceError(Exception):
    """Custom exception for lead service errors."""

    def __init__(self, message: str, status_code: int = 500, data: dict | None = None):
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)
