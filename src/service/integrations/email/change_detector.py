"""Email row change detection for normalized provider payloads."""

from __future__ import annotations

from typing import Any

from src.domain.models.organisation_tables.sync.email_models import Email, EmailFolder
from src.service.integrations.email.types import NormalizedEmailFolder, NormalizedEmailMessage


def has_meaningful_folder_changes(
    existing: EmailFolder,
    incoming: NormalizedEmailFolder,
) -> bool:
    """Return whether an incoming folder/label should rewrite `email_folders`.

    Folder writes are skipped when key metadata is unchanged, which keeps
    incremental sync calls from creating noise on every run.
    """
    fields = incoming.fields

    checks: list[tuple[Any, Any]] = [
        (existing.folder_name, fields.get("folder_name")),
        (existing.folder_type, fields.get("folder_type")),
    ]

    for existing_val, incoming_val in checks:
        if existing_val is None and incoming_val is None:
            continue
        if existing_val != incoming_val:
            return True

    incoming_total = fields.get("total_count", 0)
    incoming_unread = fields.get("unread_count", 0)

    if existing.total_count != incoming_total:
        return True
    return bool(existing.unread_count != incoming_unread)


def has_meaningful_message_changes(
    existing: Email,
    incoming: NormalizedEmailMessage,
) -> bool:
    """Return whether an incoming message should rewrite `emails`.

    Message updates are limited to fields that materially affect list rendering
    and follow-up logic; labels are treated as a set so order changes do not
    trigger writes.
    """
    fields = incoming.fields

    checks: list[tuple[Any, Any]] = [
        (existing.subject, fields.get("subject")),
        (existing.from_email, fields.get("from_email")),
        (existing.is_read, fields.get("is_read")),
        (existing.is_starred, fields.get("is_starred")),
        (existing.is_important, fields.get("is_important")),
        (existing.is_draft, fields.get("is_draft")),
    ]

    for existing_val, incoming_val in checks:
        if existing_val is None and incoming_val is None:
            continue
        if existing_val != incoming_val:
            return True

    existing_labels = existing.labels or []
    incoming_labels = fields.get("labels") or []
    if set(existing_labels) != set(incoming_labels):
        return True

    existing_folder_metadata = existing.email_metadata or {}
    existing_parent_folder = existing_folder_metadata.get("parent_folder_id")
    return bool(
        incoming.external_folder_id
        and existing_parent_folder
        and incoming.external_folder_id != existing_parent_folder
    )
