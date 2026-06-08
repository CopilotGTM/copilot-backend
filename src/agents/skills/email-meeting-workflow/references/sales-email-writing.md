# Sales Email Writing

Use this guide when drafting sales emails, follow-ups, re-engagement notes, or meeting
asks. It is adapted from public sales-email skill and prompt patterns, then constrained
for CopilotGTM's CRM evidence workflow.

## Source Signals

- MCP Market's Cold Email Writer listing emphasizes brief, human-centric B2B prospecting
  emails, peer-like tone, personalized opening lines, low-friction CTAs, follow-up
  cadences, 2-4 word subject lines, and 4-level personalization signals. It lists about
  13,892 GitHub stars.
- `sales-skills/sales` is a router/catalog repo with 42 GitHub stars. Its useful pattern
  is routing a plain-language sales objective to the right specialized workflow.
- `codebasics/project-genai-cold-email-generator` has 134 GitHub stars and reinforces a
  simple pattern: extract company context, map it to relevant proof, then generate a
  personalized email.

## Writing Rules

- Sound like a peer, not a sales bot.
- Prefer 60-120 words unless the user asks for a longer email.
- Use short subject lines, usually 2-5 words.
- Open with a specific context signal from the recipient, account, deal, meeting, email,
  or public company fact.
- Tie the message to the recipient's likely priority, risk, or next step.
- Use one clear ask. Prefer low-friction CTAs like a short meeting, quick confirmation,
  or feedback on a specific point.
- Avoid hype, exaggerated metrics, fake familiarity, and generic "hope you're well" filler.

## Evidence to Reference

Prefer first-party sales context over generic personalization:

| Evidence | How to use it |
| --- | --- |
| Deal memory | Summarize the current story and avoid re-discovering known context |
| Risks and blockers | Frame urgency or ask for help resolving a specific blocker |
| Stakeholder pain points | Lead with what matters to that person, not generic product value |
| Stakeholder motivation/goals | Tie the ask to the recipient's desired outcome |
| Deal notes | Reference promised follow-ups, internal observations, or account-team context |
| Meeting transcript or summary | Use quotes, decisions, objections, and action items |
| Prior email history | Avoid repeating asks and continue the current thread logically |
| Tasks and next steps | Turn open work into a clear owner/date/action ask |
| Attachments/internal docs | Ground claims in documents the user has provided or referenced |
| Public company facts | Add timely context only when relevant and verified |

Do not put internal-only language, private notes, or raw system labels in the external email.
Translate evidence into customer-safe wording.

## Structure

Use this default shape:

1. Subject line.
2. Context hook: why this is relevant now.
3. Value or reason: one sentence grounded in evidence.
4. Ask: one low-friction CTA.
5. Optional sign-off.

## Personalization Levels

Use the strongest available level:

| Level | Evidence |
| --- | --- |
| 1 | Role, company, industry, or account context |
| 2 | Recent meeting, email, note, CRM stage, or known initiative |
| 3 | Specific quote, objection, pain point, stakeholder goal, or active risk |
| 4 | Trigger event plus tailored recommendation or next step |

If the available evidence is only level 1, keep the email modest and do not imply deeper
knowledge.

## Follow-Up Rules

- Do not repeat the first email.
- Change the angle: risk, useful context, social proof, meeting recap, or concrete next step.
- Keep each follow-up shorter than the first unless adding requested detail.
- Stop short of pressure language.

## Output

For drafts, use:

```markdown
Subject: ...

Hi ...,

...
```

If evidence is missing, add a short note after the draft:

```markdown
Missing before send: exact recipient email / sender mailbox / meeting time.
```
