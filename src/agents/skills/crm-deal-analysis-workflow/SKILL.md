---
name: crm-deal-analysis-workflow
description: Use when a chat, Teams, Slack, WhatsApp, web, or voice user asks to analyze CRM, deals, contacts, stakeholders, participants, accounts, meetings, emails, notes, tasks, risks, next steps, or deal health without changing CRM data.
---

# CRM Deal Analysis Workflow

## Purpose

Use this skill for read-only CRM and deal intelligence. The goal is to understand the
deal, people, activity, risks, and recommended next steps before answering.

Do not use this skill for CRM mutations. If the user asks to create, update, complete,
assign, add, or save CRM data, use `crm-change-workflow`.

Follow the GeneralAgent Context Resolution Gate before asking which deal, account,
contact, stakeholder, meeting, email, note, task, or segment the user means. Resolve from
active context and read-only tools first; ask only with candidates or after lookup fails.

## Fast Decision

| User intent | Action |
| --- | --- |
| "Analyze this deal" | Build a deal evidence pack, then synthesize |
| "Who is the champion/economic buyer/blocker?" | Gather people, activity, and evidence |
| "What happened with this contact?" | Analyze CRM contact plus meetings/emails/notes |
| "What are the risks/next steps?" | Gather deal activity, identify gaps, recommend actions |
| "What should I know before a meeting?" | Gather meeting, deal, account, people, and recent activity |
| "Create/update/add/complete..." | Stop and use `crm-change-workflow` |

## Key Concepts

Use these terms consistently:

| Term | Meaning |
| --- | --- |
| Contact | A CRM person record, usually tied to an account/company |
| Lead | A CRM prospect or inbound signal that may not yet be qualified into a contact, account, or deal |
| Stakeholder | A person relevant to a deal, with sales meaning such as champion, buyer, evaluator, blocker, influencer, or user |
| Participant | A person seen in an interaction such as a meeting, email, call, or message |
| Deal participant | A participant connected to a specific deal through meetings, emails, notes, tasks, or CRM associations |
| Account | The company or organization connected to one or more contacts and deals |

Contacts, stakeholders, and participants can overlap, but they are not the same:

- A lead may exist before CRM qualification and may not have a matching contact,
  account, or deal yet.
- A contact may exist in CRM but never join a meeting.
- A participant may join a meeting but not yet exist as a CRM contact.
- A stakeholder is a sales interpretation of a person based on role, influence,
  activity, and deal relevance.

## Evidence Strategy

Start broad enough to avoid missing context, then narrow.

Prioritize evidence in this order:

1. Current conversation and explicit user request.
2. Active deal/account/contact context.
3. CRM records: deals, accounts, contacts, stakeholders, tasks, notes.
4. Meetings and transcripts.
5. Emails and communication history.
6. Internal knowledge base and uploaded files.
7. Web search only when current public/company/market facts matter.

Never invent missing facts. If evidence is absent, say what is missing and what was checked.

## Tool Categories to Consider

Use the available tool whose description matches the needed evidence.

| Need | Tool category |
| --- | --- |
| Find or confirm a deal | Deal search/list/detail tools [SearchDeals, ListDeals, GetDealDetails] |
| Find or confirm a lead | Lead search/list tools [ListLeads] |
| Understand account/company context | Account/company search and knowledge tools [SearchAccounts, InternalKnowledgeBase] |
| Understand a person | Contact, stakeholder, participant, and user lookup tools [SearchContacts, ListDealStakeholders] |
| Recent engagement | Meeting, transcript, email, note, and task tools [ListDealMeetings, GetMeetingTranscript, ListDealEmails, ListDealNotes] |
| Inbound signals from one mailbox | Connected mailbox and mailbox email tools [ListConnectedEmailAccounts, SearchMailboxEmails] |
| Deal activity | CRM notes, CRM tasks, timeline, activity, and history tools [ListDealNotes, ListDealTasks, GetDealChangeHistory] |
| Evidence from docs | Internal knowledge base and attachment tools [InternalKnowledgeBase] |
| Public/company facts | Web or internet search tools [InternetSearch] |
| Structured analysis | Relevant analysis skills such as MEDPIC, stakeholder analysis, risk analysis, or briefing skills |

Use the best available tools for each data signal. Bracket hints are examples, not hard constraints.

If a specialized analysis skill exists for the user's requested framework, load that skill
after gathering enough context to know it applies.

## Four-Question Analysis Frame

For most CRM/deal analysis, answer these internally before writing:

1. What entity is the user asking about?
   Identify deal, account, contact, stakeholder, meeting, or task.

2. What evidence do we have?
   List the CRM records, meetings, emails, notes, tasks, docs, and web facts found.

3. What does the evidence mean?
   Separate facts from interpretation. Name confidence level when evidence is thin.

4. What should the user do next?
   Recommend specific actions, owners, or follow-up questions.

## Common Flows

### Deal Health Analysis

1. Resolve the deal.
2. Get deal basics: stage, amount, close date, owner, account, status.
3. Gather recent activity: meetings, emails, notes, tasks, timeline.
4. Identify people: contacts, stakeholders, participants, internal owner.
5. Check engagement recency and momentum.
6. Identify risks, blockers, missing buyer coverage, and next steps.
7. Answer with status, evidence, risk, and recommended actions.

### Contact or Stakeholder Analysis

1. Resolve the person from CRM contact, stakeholder, meeting participant, or email participant.
2. Check their account and deal associations.
3. Gather their recent meetings, emails, notes, and tasks.
4. Infer role from evidence: champion, economic buyer, technical evaluator,
   influencer, blocker, legal/procurement, end user, or unknown.
5. State confidence and evidence. Do not over-label a person from weak evidence.
6. Recommend how to engage them next.

### Lead Analysis

1. Resolve the lead from CRM leads, current context, or the user's provided signal.
2. For inbound leads from email, resolve the exact connected mailbox first and use
   `search_mailbox_emails`; do not read all connected mailboxes unless the user asked
   for all of them.
   Mailbox thread discovery is broader than CRM: a conversation may begin before a
   lead or deal exists. Use CRM matches as enrichment and prioritisation, not as a
   prerequisite for including the email.
3. Check whether the lead already maps to a contact, account, company, or deal.
4. Gather captured message, source, owner, status, company, and recent activity if available.
5. Identify qualification gaps such as missing buyer identity, company fit, interest,
   timeline, budget, or next owner action.
6. Recommend the smallest useful next action without creating or updating CRM data.

### Pre-Meeting Briefing

1. Resolve the meeting and related deal/account.
2. Gather attendees and map them to contacts/stakeholders when possible.
3. Gather recent deal activity and open tasks.
4. Summarize current deal state, likely agenda, risks, and suggested questions.
5. Keep output concise for the channel.

### "Who are the stakeholders?"

1. Resolve the deal.
2. List CRM stakeholders, CRM contacts, meeting participants, and email participants.
3. Merge duplicate people when evidence clearly matches.
4. Classify each person by relationship to the deal.
5. Mark uncertain roles as "Unknown" instead of guessing.

### Risk and Next-Step Analysis

1. Resolve the deal/account.
2. Gather stage, close date, amount, last activity, open tasks, notes, emails, and meetings.
3. Look for stale activity, missing buyer, weak champion, unresolved objections,
   no next meeting, pricing/procurement/legal gaps, and timeline mismatch.
4. Recommend the smallest useful next actions.

## Output Patterns

For a quick answer:

```markdown
Short answer: ...

Evidence:
- ...

Next step:
- ...
```

For deal analysis:

```markdown
## Summary
...

## Evidence
- ...

## People
- ...

## Risks
- ...

## Recommended next steps
- ...
```

For stakeholder/contact analysis:

```markdown
## Person
...

## Role assessment
...

## Evidence
- ...

## Recommended engagement
- ...
```

## Confidence Rules

- High confidence: multiple direct CRM or conversation evidence points agree.
- Medium confidence: one direct source plus supporting activity.
- Low confidence: inferred from weak, stale, or indirect evidence.
- Unknown: evidence is missing or contradictory.

Say "unknown" instead of forcing a role.

## Anti-Patterns

Avoid these behaviors:

- Treating every meeting participant as a stakeholder.
- Treating every CRM contact as active in the deal.
- Calling a person champion without evidence of advocacy.
- Using web facts when internal CRM/activity evidence is required.
- Mutating CRM data from an analysis flow.
- Producing a long report for a simple lookup.
- Hiding missing evidence.
