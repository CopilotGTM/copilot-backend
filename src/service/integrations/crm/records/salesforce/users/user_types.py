"""DTOs for Salesforce users record sync."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from src.service.integrations.crm.records.common.record_sync_types import (
    RecordSyncResult,
)


@dataclass(slots=True, frozen=True)
class NormalizedSalesforceUser:
    external_id: str
    email: str | None
    first_name: str | None
    last_name: str | None
    full_name: str | None
    user_role: str | None
    is_active: bool
    source_created_at: datetime | None
    source_modified_at: datetime | None
    properties: dict[str, Any]
    data_payload: dict[str, Any]


@dataclass(slots=True, frozen=True)
class UserMatchResult:
    user: NormalizedSalesforceUser
    matched_app_user_id: UUID | None


@dataclass(slots=True)
class UserWriteResult:
    users_created: int = 0
    users_updated: int = 0
    users_reactivated: int = 0
    users_deactivated: int = 0
    users_matched_public: int = 0
    users_minted_uuid: int = 0
    skipped_dup_email: int = 0


@dataclass(slots=True)
class UserSyncResult(RecordSyncResult):
    fetched_users: int = 0
    users_created: int = 0
    users_updated: int = 0
    users_reactivated: int = 0
    users_deactivated: int = 0
    users_matched_public: int = 0
    users_minted_uuid: int = 0
    skipped_malformed: int = 0
    skipped_dup_email: int = 0
    app_user_index_size: int = 0
    deal_backfill_rows_updated: int = 0
    deal_backfill_per_op: dict[str, int] = field(default_factory=dict)
    error_class: str | None = None
    error_message: str | None = None

    def to_metadata(self) -> dict[str, Any]:
        payload = self.base_metadata()
        payload.update(
            {
                "fetched_users": self.fetched_users,
                "users_created": self.users_created,
                "users_updated": self.users_updated,
                "users_reactivated": self.users_reactivated,
                "users_deactivated": self.users_deactivated,
                "users_matched_public": self.users_matched_public,
                "users_minted_uuid": self.users_minted_uuid,
                "skipped_malformed": self.skipped_malformed,
                "skipped_dup_email": self.skipped_dup_email,
                "app_user_index_size": self.app_user_index_size,
                "deal_backfill_rows_updated": self.deal_backfill_rows_updated,
                "deal_backfill_per_op": self.deal_backfill_per_op,
                "error_class": self.error_class,
                "error_message": self.error_message,
            }
        )
        return payload
