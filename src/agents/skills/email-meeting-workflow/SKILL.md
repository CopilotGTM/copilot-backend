---
name: email-meeting-workflow
description: Use when a chat, Teams, Slack, WhatsApp, web, or voice user asks to write or send an email, draft sales outreach or follow-up copy, schedule/create/send a meeting invite, send a meeting to people or all stakeholders, list/resolve meetings, send a meeting bot, or resolve email/meeting recipients from CRM context.
---

# Email Meeting Workflow

## Purpose

Use this skill for email and meeting actions that depend on conversation, deal,
stakeholder, contact, meeting, email, or attachment context.

Do not make the user copy data from the screen when the system can resolve it from
the active conversation, deal context, CRM records, prior emails, meetings, or files.

## Fast Decision

| User intent | Action |
| --- | --- |
| Write or improve an email | Gather context, load `references/sales-email-writing.md`, then draft |
| Send an email | Resolve recipients and content, then use `send_email` |
| Schedule/send a meeting invite | Resolve title, time, attendees, and context, then use `send_calendar_invite` |
| "Send meeting/email to all stakeholders" | Resolve the deal, gather stakeholders, confirm recipients if needed |
| "Send it to Sarah/the champion/the buyer" | Resolve the person from stakeholders, contacts, meetings, emails, or conversation |
| "Send a bot to my meeting" | List meetings, resolve one clear event, then use the meeting bot tool |
| Missing critical fields | Ask one bundled clarification that covers every blocker |

## Context Resolution Order

Resolve missing details before asking the user:

1. Current and previous conversation turns.
2. Active deal, account, contact, or meeting context.
3. CRM records: deals, contacts, stakeholders, notes, tasks.
4. Recent meetings, attendees, transcripts, summaries, and follow-ups.
5. Recent emails and thread details.
6. Attachments and internal knowledge when the user refers to files or docs.
7. Web only for public/company facts needed to personalize outreach.

Never invent names, emails, meeting times, or commitments. If several required fields
are missing or ambiguous, ask for them together in one message instead of one-by-one
follow-ups.

## Context You Can Reference

When writing a specific email or meeting invite, use relevant first-party evidence:

| Context | Use it for |
| --- | --- |
| Deal memory | Current narrative, account context, decision process, open threads |
| Deal details and summary | Stage, amount, close date, owner, account, next steps |
| Existing risks and blockers | Urgency, unresolved objections, risk-aware follow-up |
| Stakeholders and stakeholder details | Recipient role, stance, decision power, motivation |
| Stakeholder pain points | Personalized hooks, value framing, agenda topics |
| Deal notes | Internal context, promised follow-ups, account-team observations |
| Meeting details, summaries, transcripts, follow-ups | Recaps, quotes, decisions, action items, agenda continuity |
| Email history and thread details | Prior asks, unanswered messages, tone, recipients, commitments |
| Tasks and next steps | Operational asks, owners, deadlines |
| Attachments and internal knowledge | Product proof, pricing context, documents the user references |
| Public web/company facts | Only for current public triggers or company-specific personalization |

Reference only what the evidence supports. Do not cite or imply sensitive internal notes in
external-facing copy unless the user asks to include that detail and it is appropriate to share.

## Recipient Resolution

Use the best available tools for the recipient phrase:

| Phrase type | Resolution path |
| --- | --- |
| "all stakeholders" | Resolve active deal, then gather deal stakeholders |
| Role phrase like "champion", "buyer", "legal" | Gather stakeholders and stakeholder details, then select evidence-backed matches |
| Person name | Search stakeholders first when deal context exists, then contacts/CRM users/internal users |
| Company/account group | Resolve account/deal first, then contacts/stakeholders |
| Meeting participants | Resolve meeting, then use attendee list |
| Prior email participants | Resolve email/thread, then use from/to/cc |

For calendar invites, `send_calendar_invite` resolves attendee names against approved
deal stakeholders, CRM contacts, CRM users when available, and active approved internal users
inside the organisation. If the tool accepts raw attendee text and `deal_id`, prefer passing
the user's phrase plus the resolved deal rather than forcing manual expansion or asking for an
email first. The invite tool will return confirmation or clarification if identity is uncertain.

## Email Workflow

1. Classify the email:
   - New email, follow-up, re-engagement, meeting recap, prep note, stakeholder-specific ask,
     or cold/warm outbound.
   - Do not use `send_email` for replies, drafts, attachments, scheduled sends, shared
     mailboxes, or thread continuation.

2. Gather enough context:
   - Recipient identity and email.
   - Deal/account/person context.
   - Deal memory, risks and blockers, stakeholder pain points, deal notes, and next steps
     when they make the message more specific.
   - Prior meeting/email evidence when the email references history.
   - Clear ask or desired next step.

3. Draft before sending when content is not explicit.
   - Load `references/sales-email-writing.md` for outbound, follow-up, or persuasive sales copy.
   - Keep copy short, specific, peer-like, and tied to the recipient's world.
   - Include a low-friction CTA.

4. Send only when required fields are known:
   - `to`, `subject`, and `body` are required.
   - Ask only for missing required fields.
   - If recipients came from names or the sender mailbox is implicit, confirm sender and
     recipients before sending.

## Meeting Invite Workflow

1. Resolve the meeting purpose and title.
   - Infer a reasonable title from the stated purpose, active deal, prior message, or agenda.
   - If only a person is clear, use a neutral title such as "Meeting with Saksham" and
     show it in the confirmation.
   - Do not ask for a title first when attendee, time, and meeting intent are clear.
   - Ask for a title only when no purpose, attendee, or context exists.

2. Resolve time:
   - Start and end time are required.
   - Use conversation timezone metadata when available.
   - Pass local time expressions as the user said them; do not convert them to UTC.
   - Preserve local weekday intent, e.g. "Saturday 10 PM" means the user's local Saturday.
   - If the date is omitted and "today" is not clearly intended, ask for the date in the same
     clarification as any missing title or attendee details.

3. Resolve attendees:
   - Use recipient resolution rules.
   - For "all stakeholders", pass the raw phrase and resolved `deal_id` when supported.
   - Confirm resolved attendees before sending when the tool requests confirmation.

4. Add useful context:
   - Include concise body/agenda when the user asks for a meeting with context.
   - Pull agenda points from deal notes, recent meetings, emails, or current conversation.

5. Send only after blockers are cleared:
   - Use `send_calendar_invite`.
   - Once title, time, attendees, sender, and timezone are resolved, present the final
     confirmation payload and wait.
   - If the user only provides missing details, do not treat that as approval. Call
     `send_calendar_invite` again with those details and `confirmed=False` so the final
     confirmation card is shown.
   - On explicit user approval of that final confirmation, call again with resolved attendees
     and `confirmed=True`.
   - If sender mailbox, title, date/time, or attendee identity is missing, ask for all
     missing fields in one message.

### Meeting Invite Examples

| User request | Preferred handling |
| --- | --- |
| "Create a meeting with Saksham from 8 to 9 PM" | Use title "Meeting with Saksham"; pass `attendees=["Saksham"]` so the invite tool searches stakeholders, CRM contacts, CRM users, and internal users. If date and sender are missing, ask once: "Should I schedule this for today from 8-9 PM, and which sender mailbox should I use?" |
| "Schedule Exiter review with Saksham Bhutani Saturday 8-9 PM" | Call `send_calendar_invite` with title "Exiter review", local Saturday start/end, and attendee name. If Saksham resolves to an internal user, show confirmation before sending. |
| "Send meeting to all stakeholders tomorrow 4 PM" | Resolve active deal, call `send_calendar_invite` with `attendees=["all stakeholders"]` and `deal_id`; confirm the expanded recipient list before sending. |
| "Set up a call with legal and the champion" | Resolve deal stakeholders by role evidence. If multiple legal/champion matches exist, ask one question listing all ambiguous roles and missing time/title fields together. |

Do not ask sequentially like: title first, then attendee email, then date. Collect all known
fields, use resolver tools for names, then ask one concise clarification for the remaining
blockers, including sender mailbox if the user has multiple connected calendar accounts.
After blockers are resolved, show the final confirmation card before sending.

## Meeting Bot Workflow

- Use only for the user's connected calendar.
- For "my next meeting", first return the tool's confirmation preview and dispatch only
  after the user confirms.
- For named meetings, list meetings and resolve one clear event.
- If multiple meetings match, ask the user to choose before sending the bot.

## Safety Rules

- Preserve organization and user boundaries.
- Never send to guessed recipients.
- Do not expose internal IDs unless explicitly asked.
- Do not send emails or invites when the user only asked for a draft.
- If the user says "send it" after a draft, use the current draft and current resolved
  recipients; do not regenerate from scratch unless they ask for edits.

## Response Rules

- If drafting, return the draft only plus any missing-send blockers.
- If sending requires confirmation, show sender, recipients, subject/title, time, and key body.
- If sent, state what was sent and to whom.
- If blocked, ask exactly one focused question that includes every missing or ambiguous field.

## Reference Loading

- Use `references/sales-email-writing.md` when writing outbound, follow-up, re-engagement,
  or persuasive sales emails.
