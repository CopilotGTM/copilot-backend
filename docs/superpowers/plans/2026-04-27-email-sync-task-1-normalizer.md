# Email Sync Task 1 Package Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the email sync package and move provider normalization, change detection, and database writes out of the monolithic `EmailSyncService`.

**Architecture:** Add `src/service/integrations/email/` with provider-neutral DTOs, an `EmailPayloadNormalizer`, pure change-detector functions, an `EmailSyncWriter`, and the package-owned orchestration service. Keep top-level `src/service/integrations/email_sync_service.py` as a compatibility shim and preserve existing helper methods as wrappers so current tests and import paths keep running.

**Tech Stack:** Python 3.11, FastAPI backend conventions, SQLModel models, pytest, Ruff.

---

### Task 1: Split Email Sync Into Package Modules

**Files:**
- Create: `src/service/integrations/email/__init__.py`
- Create: `src/service/integrations/email/types.py`
- Create: `src/service/integrations/email/normalizer.py`
- Create: `src/service/integrations/email/change_detector.py`
- Create: `src/service/integrations/email/writer.py`
- Create: `src/service/integrations/email/sync_service.py`
- Modify: `src/service/integrations/email_sync_service.py`
- Test: `tests/service/integrations/email/test_change_detector.py`
- Test: `tests/service/integrations/email/test_normalizer.py`
- Test: `tests/service/integrations/email/test_writer.py`
- Test: `tests/service/integrations/test_email_sync_service_gmail.py`
- Test: `tests/service/integrations/test_email_sync_service_incremental.py`

- [x] **Step 1: Write the package tests through existing behavior**

Use the existing Gmail/Outlook extraction tests as the red/green guard:

```bash
uv run pytest tests/service/integrations/test_email_sync_service_gmail.py -q
```

Expected before implementation: PASS, proving the current behavior is covered.

- [x] **Step 2: Add provider-neutral DTOs**

Create frozen dataclasses:

```python
@dataclass(frozen=True)
class NormalizedEmailFolder:
    external_id: str | None
    provider: str
    fields: dict[str, Any]
    data_payload: dict[str, Any]
    debug_sample: dict[str, Any]

@dataclass(frozen=True)
class NormalizedEmailMessage:
    external_id: str | None
    provider: str
    external_folder_id: str | None
    fields: dict[str, Any]
    data_payload: dict[str, Any]
    debug_sample: dict[str, Any]
```

- [x] **Step 3: Move parsing helpers into `EmailPayloadNormalizer`**

Move these helpers from `EmailSyncService` into `src/service/integrations/email/normalizer.py`:

```text
_extract_folder_id
_extract_message_id
_extract_folder_fields
_extract_outlook_folder_fields
_classify_folder_type
_extract_gmail_folder_fields
_extract_generic_folder_fields
_extract_message_fields
_extract_outlook_message_fields
_extract_gmail_message_fields
_extract_generic_message_fields
_parse_datetime
_html_to_text
_extract_external_folder_id_from_message
_parse_email_address
_parse_email_list
```

Expose public methods without leading underscores:

```python
normalize_folder(folder_data, provider) -> NormalizedEmailFolder
normalize_message(message_data, provider) -> NormalizedEmailMessage
extract_folder_id(folder_data, provider) -> str | None
extract_message_id(message_data, provider) -> str | None
extract_folder_fields(folder_data, provider) -> dict[str, Any]
extract_message_fields(message_data, provider) -> dict[str, Any]
extract_external_folder_id_from_message(message_data, provider) -> str | None
```

- [x] **Step 4: Wire `EmailSyncService` to the normalizer**

Add `self._normalizer = EmailPayloadNormalizer()` in `EmailSyncService.__init__`.

Keep compatibility wrappers on `EmailSyncService` for existing tests and current staged code:

```python
@classmethod
def _extract_gmail_message_fields(cls, message_data: dict[str, Any]) -> dict[str, Any]:
    return EmailPayloadNormalizer.extract_gmail_message_fields(message_data)
```

Do this for all old normalization helper names.

- [x] **Step 5: Extract change detection**

Move meaningful-change checks into `src/service/integrations/email/change_detector.py` as pure functions over normalized DTO fields. Keep the service wrapper methods as compatibility methods that normalize incoming provider payloads before delegating to the pure functions.

- [x] **Step 6: Extract database writes**

Move folder/message upsert construction, batch writes, and folder-id resolution into `EmailSyncWriter`. Keep `EmailSyncService` responsible for orchestration, sync-history bookkeeping, and provider run counters.

- [x] **Step 7: Move orchestration into the package**

Move the service implementation to `src/service/integrations/email/sync_service.py`. Replace top-level `src/service/integrations/email_sync_service.py` with a compatibility shim that re-exports `EmailSyncService` and the legacy `_parse_email_*` helpers.

- [x] **Step 8: Preserve production failure boundaries**

Keep incremental message sync on the batched writer path for the normal case, but fall back to item-level writes when a batch fails so one malformed row does not fail the whole incremental run. Bound message upsert statements with writer-level chunking.

- [x] **Step 9: Verify focused behavior**

Run:

```bash
uv run pytest tests/service/integrations/test_email_sync_service_gmail.py -q
uv run pytest tests/service/integrations/email tests/service/integrations/test_email_sync_service_incremental.py -q
```

Expected: both commands pass.

- [x] **Step 10: Verify style for touched files**

Run:

```bash
uv run ruff check src/service/integrations/email src/service/integrations/email_sync_service.py tests/service/integrations/email tests/service/integrations/test_email_sync_service_gmail.py tests/service/integrations/test_email_sync_service_incremental.py
```

Expected: `All checks passed!`
