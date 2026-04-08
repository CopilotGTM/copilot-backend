"""Attendee display-name resolution with email-local-part fallback.

Google Calendar only populates `displayName` for attendees the authenticated
user already knows (saved Contact, same Workspace directory, or "Other
contact" from prior correspondence). Microsoft Graph always populates it.

For Google-sourced events where the attendee is external, we get back an
email with no name, store NULL in `display_name`, and every consumer —
API responses, agent prompts, meeting summaries — ends up showing "Unknown"
or nothing at all.

This helper gives every backend read path a single place to get a usable
name: prefer the authoritative `display_name` if present, otherwise derive
a best-effort name from the email local-part. The DB stays truthful
(NULL = "we don't really know") while the rendered output stays useful.

Use this instead of reading `attendee.display_name` directly in any code
that renders/returns a name to a user, an LLM, or a downstream system.
"""

from __future__ import annotations

# Local-parts we deliberately refuse to turn into a "name". Generic mailboxes
# should not show up as "Noreply" or "Admin" in the UI or LLM prompts.
_GENERIC_LOCAL_PARTS = frozenset(
    {
        "noreply",
        "no-reply",
        "donotreply",
        "do-not-reply",
        "admin",
        "administrator",
        "info",
        "support",
        "help",
        "contact",
        "hello",
        "hi",
        "team",
        "sales",
        "marketing",
        "hr",
        "billing",
        "accounts",
        "accounting",
        "finance",
        "notifications",
        "alerts",
        "mailer-daemon",
        "postmaster",
        "root",
    }
)


def derive_name_from_email(email: str | None) -> str | None:
    """Derive a readable name from an email local-part.

    Examples:
        darshankumar.m@industowers.com  → "Darshankumar M"
        vikram.srivastava@airtel.com    → "Vikram Srivastava"
        naresh@deepmatrix.io            → "Naresh"
        first_last@domain.com           → "First Last"
        noreply@foo.com                 → None   (generic mailbox)
        a@b.com                         → None   (too short)
        None / ""                       → None
    """
    if not email or "@" not in email:
        return None

    local_part = email.split("@", 1)[0].strip().lower()
    if not local_part:
        return None

    if local_part in _GENERIC_LOCAL_PARTS:
        return None

    # Split on common separators used in corporate email conventions.
    raw_tokens = local_part.replace("_", ".").replace("-", ".").replace("+", ".").split(".")
    tokens = [t for t in raw_tokens if t and not t.isdigit()]

    if not tokens:
        return None

    # Single-letter-only local-parts ("a", "fl") are too ambiguous to guess from.
    if all(len(t) == 1 for t in tokens):
        return None

    name = " ".join(t.capitalize() for t in tokens)
    return name or None


def resolve_attendee_display_name(display_name: str | None, email: str | None) -> str | None:
    """Return the best available display name for an attendee.

    Prefers the authoritative `display_name` if present; otherwise falls
    back to deriving a name from the email local-part. Returns None only
    when both sources fail (e.g. Google gave us no name and the address
    is a generic mailbox).
    """
    if display_name and display_name.strip():
        return display_name.strip()
    return derive_name_from_email(email)
