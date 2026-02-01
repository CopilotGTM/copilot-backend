# Agent Tools Planning Document

**Date:** January 30, 2026  
**Status:** Planning Phase  
**Priority:** High

---

## Overview

This document outlines the planned agent tools for deal management via the GeneralAgent. These tools will enable users to create deals, add notes, and perform other deal-related actions through natural language conversations in Teams, Web Chat, and future channels (Slack).

The tools follow the **adapter architecture** pattern where the agent produces channel-agnostic responses, and channel adapters (TeamsAdapter, WebAdapter) render them appropriately.

---

## Tool Categories

### 1. Deal Creation Tools

#### 1.1 `create_deal` - Direct Deal Creation

**Purpose:** Create a deal immediately when user provides sufficient information.

**Trigger Phrases:**
- "Create a deal called [name]"
- "Add a new deal for [company] worth [amount]"
- "Create deal [name] in [pipeline] stage"

**Parameters:**
```python
{
    "name": str,                    # Required - Deal name
    "amount": float | None,         # Optional - Deal value
    "currency": str | None,         # Optional - 3-letter code (USD, EUR, etc.)
    "pipeline_id": UUID | None,     # Optional - Pipeline ID
    "stage_id": UUID | None,        # Optional - Stage ID
    "owner_user_id": UUID | None,   # Optional - Assigned owner
    "close_date": datetime | None,  # Optional - Expected close date
    "description": str | None,      # Optional - Deal description
    "account_name": str | None,     # Optional - Associated company name
    "priority": str | None,         # Optional - High/Medium/Low
}
```

**Logic Flow:**
1. Extract deal information from user message using LLM
2. Validate required fields (name is mandatory)
3. If pipeline/stage not provided, use organization's default
4. Call `DealWriteService.create_deal()`
5. Return success confirmation with deal link

**Confirmation Strategy:**
- **Direct creation:** If name + amount provided → Create immediately
- **Confirmation:** If only name provided → Show confirmation card with editable fields

**Response Types:**
- `ResponseType.TEXT` - "Deal 'Acme Corp' created successfully with value $50,000"
- `ResponseType.FORM` - Show pre-filled form for user to confirm/edit before creation

**Error Handling:**
- Missing name → Ask user for deal name
- Invalid pipeline/stage → List available pipelines/stages
- Duplicate name warning → Ask if user wants to proceed

---

#### 1.2 `show_deal_creation_form` - Guided Deal Creation

**Purpose:** Show a form when user wants to create a deal but hasn't provided details.

**Trigger Phrases:**
- "Create a new deal"
- "I want to add a deal"
- "Add deal"

**Behavior:**
1. Fetch organization's pipelines and stages
2. Fetch team members for owner assignment
3. Generate form with all available options
4. Return `ResponseType.FORM` with fields:
   - Deal Name (text, required)
   - Amount (number)
   - Currency (choice: USD, EUR, GBP, etc.)
   - Pipeline (choice from org pipelines)
   - Stage (choice based on selected pipeline)
   - Owner (choice from team members)
   - Close Date (date picker)
   - Description (multiline text)
   - Account/Company (text)

**Channel Rendering:**
- **Teams:** Adaptive Card with Input.Text, Input.Number, Input.ChoiceSet, Input.Date
- **Web:** React form component
- **Slack:** Block Kit modal (future)

---

### 2. Deal Note Tools

#### 2.1 `add_deal_note` - Direct Note Addition

**Purpose:** Add a note to a specific deal when user provides clear context.

**Trigger Phrases:**
- "Add a note to [deal name] deal"
- "Note for deal [name]: [note content]"
- "Write in [deal name]: [note content]"

**Parameters:**
```python
{
    "deal_identifier": str,         # Deal name, ID, or partial match
    "note_content": str,            # The note text
    "note_subject": str | None,     # Optional subject line
    "confirm_before_add": bool      # Whether to show confirmation
}
```

**Logic Flow:**
1. Search for deal by name/identifier using `search_deals` tool
2. **Single match with high confidence (>0.9):**
   - Add note directly
   - Return confirmation
3. **Multiple matches or low confidence:**
   - Show deal selection list
   - Ask user to confirm which deal
4. **No matches:**
   - Ask user to clarify deal name

**Deal Resolution Strategy:**
```python
# Confidence scoring
if exact_name_match:
    confidence = 1.0
elif partial_match and recent_activity:
    confidence = 0.85
elif partial_match:
    confidence = 0.6
else:
    confidence = 0.0
```

**Response Types:**
- `ResponseType.TEXT` - "Note added to 'Acme Corp' deal"
- `ResponseType.CONFIRMATION` - "Add this note to 'Acme Corp' (value $50k)?"
- `ResponseType.LIST` - "Multiple deals found. Which one?"

**Error Handling:**
- Deal not found → Suggest similar deals or ask for clarification
- Ambiguous match → Show list of matching deals
- Permission denied → Inform user they don't have access

---

#### 2.2 `show_deal_note_form` - Guided Note Addition

**Purpose:** Show a form for adding a note when deal is not clearly specified.

**Trigger Phrases:**
- "Add a note"
- "I want to write a note"
- "Create a note"

**Behavior:**
1. Fetch recent deals the user has interacted with
2. Show form with:
   - Deal (choice from recent deals + search)
   - Subject (text, optional)
   - Note Content (multiline text, required)

**Form Fields:**
```python
[
    {
        "id": "deal_id",
        "type": "choice",
        "label": "Select Deal",
        "choices": [...]  # Recent deals + "Search..." option
    },
    {
        "id": "subject",
        "type": "text",
        "label": "Subject (optional)",
        "placeholder": "e.g., Follow-up call notes"
    },
    {
        "id": "content",
        "type": "text",
        "label": "Note",
        "multiline": True,
        "required": True
    }
]
```

---

### 3. Deal Query Tools (Existing Enhancement)

#### 3.1 `search_deals` - Enhanced Deal Search

**Enhancement:** Add fuzzy matching and recent deal context.

**Current:** Exact search  
**Enhanced:** Fuzzy search + recent activity weighting

**Parameters:**
```python
{
    "query": str,                   # Search term
    "fuzzy": bool = True,          # Enable fuzzy matching
    "limit": int = 10,             # Max results
    "include_recent": bool = True  # Include recently viewed deals
}
```

**Use Case:** Used internally by `add_deal_note` to resolve deal references.

---

#### 3.2 `get_deal_details` - Deal Information

**Purpose:** Retrieve full deal details for display or confirmation.

**Use Case:** Show deal summary before adding note or performing action.

---

### 4. Utility Tools

#### 4.1 `list_pipelines_and_stages` - Pipeline Discovery

**Purpose:** Get available pipelines and stages for deal creation.

**Use Case:** Populate dropdown choices in deal creation form.

**Response:**
```python
{
    "pipelines": [
        {
            "id": UUID,
            "name": str,
            "stages": [
                {"id": UUID, "name": str, "order": int}
            ]
        }
    ]
}
```

---

#### 4.2 `list_team_members` - Team Discovery

**Purpose:** Get team members for owner assignment.

**Use Case:** Populate owner dropdown in deal creation form.

---

## Implementation Phases

### Phase 1: Core Deal Creation (Week 1)
- [ ] Implement `create_deal` tool
- [ ] Implement `show_deal_creation_form` tool
- [ ] Add to GeneralAgent tool registry
- [ ] Test in Teams with Adaptive Cards
- [ ] Test in Web Chat

### Phase 2: Deal Notes (Week 2)
- [ ] Implement `search_deals` enhancement
- [ ] Implement `add_deal_note` tool
- [ ] Implement `show_deal_note_form` tool
- [ ] Add deal resolution logic
- [ ] Test confirmation flows

### Phase 3: Polish & Edge Cases (Week 3)
- [ ] Handle duplicate deal names
- [ ] Add permission checks
- [ ] Improve fuzzy matching
- [ ] Add error recovery flows
- [ ] Documentation & testing

---

## Agent Response Flow Examples

### Example 1: Direct Deal Creation

**User:** "Create a deal called Acme Corp worth $100k"

**Agent Processing:**
1. Intent: `create_deal`
2. Extract: name="Acme Corp", amount=100000, currency="USD"
3. Validate: Name present ✓, Amount valid ✓
4. Action: Call `DealWriteService.create_deal()`
5. Response: `ResponseType.TEXT`

**Teams Output:**
```
✅ Deal created successfully!

**Acme Corp**
Value: $100,000 USD
Pipeline: Sales Pipeline
Stage: Prospecting

[View Deal] [Add Note]
```

---

### Example 2: Deal Creation with Form

**User:** "Create a new deal"

**Agent Processing:**
1. Intent: `show_deal_creation_form`
2. Fetch: Pipelines, stages, team members
3. Response: `ResponseType.FORM`

**Teams Output (Adaptive Card):**
```
┌─────────────────────────────────────┐
│  Create New Deal                    │
├─────────────────────────────────────┤
│  Deal Name *                        │
│  [____________________]             │
│                                     │
│  Amount                             │
│  [____________________]             │
│                                     │
│  Pipeline                           │
│  [Sales Pipeline ▼]                 │
│                                     │
│  Stage                              │
│  [Prospecting ▼]                    │
│                                     │
│  Owner                              │
│  [John Doe ▼]                       │
│                                     │
│  [Create Deal] [Cancel]             │
└─────────────────────────────────────┘
```

---

### Example 3: Add Note with Confirmation

**User:** "Add a note to Acme deal"

**Agent Processing:**
1. Intent: `add_deal_note`
2. Search: "Acme" → Found 1 deal (confidence: 0.95)
3. Response: `ResponseType.QUESTION`

**Teams Output:**
```
I found the deal "Acme Corp" (value: $100,000).

What note would you like to add?

[Text input field]

[Add Note] [Cancel]
```

---

### Example 4: Ambiguous Deal Reference

**User:** "Add note to Microsoft deal"

**Agent Processing:**
1. Intent: `add_deal_note`
2. Search: "Microsoft" → Found 3 deals
3. Response: `ResponseType.LIST`

**Teams Output:**
```
I found multiple deals matching "Microsoft":

1. Microsoft Enterprise ($500k) - Closed Won
2. Microsoft SMB ($50k) - Negotiation
3. Microsoft Partnership ($0) - Prospecting

Which deal would you like to add a note to?

[Deal 1] [Deal 2] [Deal 3] [Cancel]
```

---

## Technical Implementation Notes

### Tool Registration

Tools should be registered in `src/agents/tools/registry.py`:

```python
def get_tools_for_agent(agent_name: str, context: AgentContext, ...):
    if agent_name == "GeneralAgent":
        tools = [
            # Existing tools...
            CreateDealTool(context),
            ShowDealCreationFormTool(context),
            AddDealNoteTool(context),
            ShowDealNoteFormTool(context),
            # ...
        ]
```

### Tool Base Class

All tools should inherit from `BaseTool`:

```python
from src.agents.tools.base import BaseTool

class CreateDealTool(BaseTool):
    name = "create_deal"
    description = "Create a new CRM deal with specified details"
    
    async def execute(self, name: str, amount: float | None = None, ...):
        # Implementation
        pass
```

### Channel Adapter Integration

The adapter will handle rendering based on `ResponseType`:

```python
# In TeamsAdapter
def render(self, response: AgentResponse) -> ChannelPayload:
    if response.type == ResponseType.FORM:
        return self._render_form(response)
    elif response.type == ResponseType.QUESTION:
        return self._render_question(response)
    # ...
```

---

## Security Considerations

1. **Organization Isolation:** All tools must filter by `organisation_id`
2. **User Permissions:** Check user has write access before creating/updating
3. **Input Validation:** Validate all inputs before database operations
4. **Audit Logging:** Log all deal creation and note additions

---

## Future Enhancements

1. **Deal Updates:** `update_deal`, `move_deal_stage`
2. **Task Creation:** `create_deal_task`
3. **Meeting Association:** `link_meeting_to_deal`
4. **Bulk Operations:** `create_multiple_deals`
5. **Voice Input:** Speech-to-text for note content

---

## Appendix: API Reference

### DealWriteService Methods
- `create_deal(payload: dict) -> CRMDeal`
- `update_deal(deal_id: UUID, payload: dict) -> CRMDeal`

### DealNotesService Methods
- `create_note(deal_id: UUID, content: str, subject: str | None) -> CRMActivity`
- `list_notes(deal_id: UUID) -> list[CRMActivity]`

### DealsAccessService Methods
- `search_deals(query: str, ...) -> list[CRMDeal]`
- `get_deal_by_id(deal_id: UUID) -> CRMDeal | None`
