"""DTOs for HubSpot users (owners) record sync.

Identity design: `user_id` is the shared identifier between `public.users`
and `crm_users`, but the sync never writes `public.users`. For each HubSpot
owner:

  - If the email matches a `public.users` row in the SAME org → reuse that
    row's `user_id` as the `crm_users.user_id`.
  - Otherwise (no email, or no match) → mint `uuid4()`. The row lives in
    `crm_users` only. When an app user with this email later signs up, the
    signup flow is expected to reuse that same `user_id` so the two tables
    converge without a renumbering pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from src.service.integrations.crm.records.common.record_sync_types import (
    RecordSyncResult,
)


@dataclass(slots=True, frozen=True)
class NormalizedHubSpotOwner:
    """A HubSpot owner after normalisation.

    `email` is already lower-cased and trimmed. `team_ids` is deduplicated,
    insertion-order preserved. `properties` and `data_payload` are the two JSON
    bags we store on `crm_users` — `properties` is a hand-curated subset for
    cheap indexed queries, `data_payload` is the raw HubSpot record for replay.
    """

    external_id: str
    email: str | None
    first_name: str | None
    last_name: str | None
    full_name: str | None
    user_role: str | None
    team_ids: tuple[str, ...]
    primary_team_id: str | None
    source_created_at: datetime | None
    source_modified_at: datetime | None
    properties: dict[str, Any]
    data_payload: dict[str, Any]


@dataclass(slots=True, frozen=True)
class OwnerMatchResult:
    """Owner paired with an optional `public.users` match.

    `matched_app_user_id is None` means "no existing app user for this email";
    the writer reuses an existing CRM row with the same email when present,
    otherwise it mints one fresh UUID for that canonical email identity.
    """

    owner: NormalizedHubSpotOwner
    matched_app_user_id: UUID | None


@dataclass(slots=True)
class UserWriteResult:
    """Counters returned from `HubSpotUserWriter.apply()`."""

    users_created: int = 0
    users_updated: int = 0
    users_reactivated: int = 0
    users_deactivated: int = 0
    # Provider owners matched to an existing public.users row. The CRM user may
    # be newly created or an existing local row that only needed provider mapping.
    users_matched_public: int = 0
    # Provider-only CRM users created with fresh local UUIDs.
    users_minted_uuid: int = 0
    # Should stay zero in normal sync: duplicate provider owners with the same
    # email are mapped to the same local CRM user instead of being dropped.
    skipped_dup_email: int = 0


@dataclass(slots=True)
class UserSyncResult(RecordSyncResult):
    """End-to-end sync summary for HubSpot users record sync."""

    fetched_owners: int = 0
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
                "fetched_owners": self.fetched_owners,
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
