# Business Trigger Experience — Two-Minute Summary

## What are we building?

Users can choose when an agent runs—for example, when a deal is created or an email is received. The screen should use familiar business names. Users should never enter internal system IDs.

## What does “Run as me” mean?

The agent runs with the permissions of the person creating the trigger.

- **Deals:** applies to deals you own or created.
- **Contacts and accounts:** applies to records you own.
- **Email:** applies to active email accounts connected by you.
- **Meetings:** applies to meetings you organise or own.
- **Agent runs:** applies to runs created by you.

This scope will be displayed as a locked **Applies to** rule. It is not an optional filter and cannot be accidentally removed.

For Deal Created, we should not silently add **Owner is me** as a filter. That would exclude a deal you created and later assigned to another person. Instead, the screen will show **Deals you own or created**. If you want only owned deals, you can add **Owner is me** with one click.

## What does “Run as organisation” mean?

The agent runs for all matching organisation records or connected accounts. Only organisation admins can select this option. Members cannot create or submit organisation-wide triggers.

Organisation execution must be compatible with the agent’s tools and skills. If unavailable, the screen will explain why.

## How should Email Received work?

With **Run as me**, the default is **All my connected mailboxes**. If you connected more than one account, you can choose addresses such as `nikhil@company.com` or `sales@company.com` from a mailbox picker.

Optional filters can then narrow the emails by sender, recipient, sender domain, attachment, or related deal. Email addresses remain normal searchable inputs because external senders may not already exist in the CRM.

## What happens to IDs?

The system may store IDs internally, but users see only readable labels:

- Deal name instead of Deal ID
- Contact name and email instead of Contact ID
- Company name and domain instead of Account ID
- Mailbox address instead of Integration ID
- Agent and trigger names instead of Run or Trigger IDs

Technical fields such as Actor ID, Source Event ID, Signal ID, Run ID, and duplicate message or meeting IDs will be removed from the normal filter experience.

## What happens next?

First, checkpoint the current trigger foundation after verification. Then improve filters gradually: Deal first, Email second, Contact and Account third, followed by Meeting, AI insights, and Agent runs. Each phase will be tested and committed separately; the current work does not need to be rebuilt.
