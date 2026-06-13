"""Audit duplicate active contact participants before uniqueness hardening."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from dotenv import load_dotenv
from sqlalchemy import text

from scripts.org_admin_session import acquire_org_admin_async_session

EMAIL_DUPLICATES_SQL = """
SELECT organisation_id, deal_id, lower(btrim(related_email)) AS email, count(*)
FROM crm_deal_participants
WHERE organisation_id = :organisation_id
  AND participant_type = 'contact'
  AND related_email IS NOT NULL
  AND btrim(related_email) <> ''
  AND is_deleted = false
GROUP BY organisation_id, deal_id, lower(btrim(related_email))
HAVING count(*) > 1
ORDER BY organisation_id, deal_id, email
"""

CONTACT_ID_DUPLICATES_SQL = """
SELECT organisation_id, deal_id, related_local_id, count(*)
FROM crm_deal_participants
WHERE organisation_id = :organisation_id
  AND participant_type = 'contact'
  AND related_local_id IS NOT NULL
  AND is_deleted = false
GROUP BY organisation_id, deal_id, related_local_id
HAVING count(*) > 1
ORDER BY organisation_id, deal_id, related_local_id
"""


@dataclass(frozen=True)
class EmailDuplicateGroup:
    organisation_id: UUID
    deal_id: UUID
    email: str
    count: int


@dataclass(frozen=True)
class ContactIdDuplicateGroup:
    organisation_id: UUID
    deal_id: UUID
    related_local_id: UUID
    count: int


@dataclass(frozen=True)
class DuplicateAuditReport:
    email_duplicates: list[EmailDuplicateGroup]
    contact_id_duplicates: list[ContactIdDuplicateGroup]

    @property
    def has_duplicates(self) -> bool:
        return bool(self.email_duplicates or self.contact_id_duplicates)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit duplicate active CRM contact participants for one organisation.",
    )
    parser.add_argument("--organisation-id", required=True, type=UUID)
    parser.add_argument(
        "--fail-on-duplicates",
        action="store_true",
        help="Exit with status 1 when duplicates are found.",
    )
    return parser


def _as_uuid(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _email_group(row: dict[str, Any]) -> EmailDuplicateGroup:
    return EmailDuplicateGroup(
        organisation_id=_as_uuid(row["organisation_id"]),
        deal_id=_as_uuid(row["deal_id"]),
        email=str(row["email"]),
        count=int(row["count"]),
    )


def _contact_id_group(row: dict[str, Any]) -> ContactIdDuplicateGroup:
    return ContactIdDuplicateGroup(
        organisation_id=_as_uuid(row["organisation_id"]),
        deal_id=_as_uuid(row["deal_id"]),
        related_local_id=_as_uuid(row["related_local_id"]),
        count=int(row["count"]),
    )


async def audit_session(session: Any, organisation_id: UUID) -> DuplicateAuditReport:
    params = {"organisation_id": organisation_id}
    email_rows = (await session.execute(text(EMAIL_DUPLICATES_SQL), params)).mappings().all()
    contact_id_rows = (
        await session.execute(text(CONTACT_ID_DUPLICATES_SQL), params)
    ).mappings().all()

    return DuplicateAuditReport(
        email_duplicates=[_email_group(dict(row)) for row in email_rows],
        contact_id_duplicates=[_contact_id_group(dict(row)) for row in contact_id_rows],
    )


async def audit_org(organisation_id: UUID) -> DuplicateAuditReport:
    report = DuplicateAuditReport(email_duplicates=[], contact_id_duplicates=[])
    async with acquire_org_admin_async_session(organisation_id) as session:
        report = await audit_session(session, organisation_id)
    return report


def print_report(report: DuplicateAuditReport) -> None:
    print(f"email_duplicate_groups={len(report.email_duplicates)}")
    for group in report.email_duplicates:
        print(
            "email_duplicate "
            f"organisation_id={group.organisation_id} "
            f"deal_id={group.deal_id} "
            f"email={group.email} "
            f"count={group.count}"
        )

    print(f"contact_id_duplicate_groups={len(report.contact_id_duplicates)}")
    for contact_group in report.contact_id_duplicates:
        print(
            "contact_id_duplicate "
            f"organisation_id={contact_group.organisation_id} "
            f"deal_id={contact_group.deal_id} "
            f"related_local_id={contact_group.related_local_id} "
            f"count={contact_group.count}"
        )


def exit_code_for_report(
    report: DuplicateAuditReport,
    *,
    fail_on_duplicates: bool,
) -> int:
    if fail_on_duplicates and report.has_duplicates:
        return 1
    return 0


async def run(args: argparse.Namespace) -> int:
    report = await audit_org(args.organisation_id)
    print_report(report)
    return exit_code_for_report(report, fail_on_duplicates=args.fail_on_duplicates)


def main() -> None:
    load_dotenv()
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
