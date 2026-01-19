# Task: Stakeholder Suggestion & Tracking Enhancements

## Status: TODO

## Problem Statement

Currently, when AI discovers stakeholders from calendar events or other sources, there's no way to track:

1. Whether a stakeholder was rejected by the user (so we keep suggesting them again)
2. Who/what added the stakeholder (user, AI, calendar sync)
3. The lifecycle state of a stakeholder suggestion

## Proposed Solution

### 1. Add Stakeholder Status Field

Track the lifecycle state of each stakeholder:

```python
class StakeholderStatus(str, Enum):
    SUGGESTED = "SUGGESTED"      # AI/Calendar suggested, pending user action
    ACCEPTED = "ACCEPTED"        # User accepted the suggestion
    REJECTED = "REJECTED"        # User rejected - don't suggest again
    ACTIVE = "ACTIVE"            # Manually added or confirmed stakeholder
```

### 2. Enhance StakeholderSource to Track Origin

Current sources:

- `CRM` - From CRM system
- `CALENDAR_DISCOVERY` - From calendar events
- `MANUAL` - User added manually

Additional tracking needed:

- `AI_SUGGESTED` - AI recommended this stakeholder

### 3. Add Tracking Fields to DealStakeholder Model

```python
# New fields to add:
status: StakeholderStatus = Field(default=StakeholderStatus.ACTIVE)
suggested_by: StakeholderSource | None = Field(default=None)  # Who suggested
suggested_at: datetime | None = Field(default=None)           # When suggested
reviewed_by_user_id: UUID | None = Field(default=None)        # Who accepted/rejected
reviewed_at: datetime | None = Field(default=None)            # When reviewed
```

### 4. New APIs Required

#### Accept Suggested Stakeholder

```
POST /{deal_id}/stakeholders/{stakeholder_id}/accept
```

#### Reject Suggested Stakeholder

```
POST /{deal_id}/stakeholders/{stakeholder_id}/reject
```

#### List Suggested Stakeholders (pending review)

```
GET /{deal_id}/stakeholders/suggestions
```

### 5. Update Calendar Discovery Logic

When discovering stakeholders from calendar events:

1. Check if email already exists in `deal_stakeholders` for this deal
2. If exists with `status=REJECTED`, skip (don't re-suggest)
3. If exists with `status=ACCEPTED/ACTIVE`, skip (already added)
4. If new, add with `status=SUGGESTED`, `source=CALENDAR_DISCOVERY`

### 6. Update AI Suggestion Logic

When AI suggests stakeholders:

1. Check rejection history before suggesting
2. Add with `status=SUGGESTED`, `source=AI_SUGGESTED`
3. Track in separate suggestions table if needed for analytics

## Benefits

1. **No repeated suggestions**: Rejected stakeholders won't be suggested again
2. **Clear provenance**: Know exactly how each stakeholder was added
3. **Audit trail**: Track who reviewed and when
4. **Better UX**: Users can review AI/calendar suggestions before they appear in main list

## Migration Considerations

- Existing stakeholders should default to `status=ACTIVE`
- Existing `source` values remain valid
- New fields should be nullable for backward compatibility

Once task is completed, move that to review.

## Related Files

- `src/domain/models/organisation_tables/sync/crm/stakeholders.py`
- `src/api/routers/crm/stakeholders_handler.py`
- `src/service/crm/stakeholders_service.py`
- `src/workers/deal_worker/services/stakeholder_service.py`
