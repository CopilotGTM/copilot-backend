"""Public imports for the email sync package."""

from src.service.integrations.email.change_detector import (
    has_meaningful_folder_changes,
    has_meaningful_message_changes,
)
from src.service.integrations.email.normalizer import EmailPayloadNormalizer
from src.service.integrations.email.sync_service import EmailSyncService
from src.service.integrations.email.types import (
    EmailFolderUpsertBatchItem,
    EmailMessageUpsertBatchItem,
    NormalizedEmailFolder,
    NormalizedEmailMessage,
)
from src.service.integrations.email.writer import EmailSyncWriter

__all__ = [
    "EmailFolderUpsertBatchItem",
    "EmailMessageUpsertBatchItem",
    "EmailPayloadNormalizer",
    "EmailSyncService",
    "EmailSyncWriter",
    "NormalizedEmailFolder",
    "NormalizedEmailMessage",
    "has_meaningful_folder_changes",
    "has_meaningful_message_changes",
]
