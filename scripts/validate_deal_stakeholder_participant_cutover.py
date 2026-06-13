"""Validate old stakeholder rows against participant-backed stakeholders.

This is a temporary pre-retirement check. It compares active approved
`deal_stakeholders` rows with active approved contact `CRMDealParticipant` rows
for one organisation before the old table is dropped in a later release.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from dotenv import load_dotenv
from sqlalchemy import text

from scripts.org_admin_session import acquire_org_admin_async_session

CUTOVER_COUNT_SQL = """
WITH old_counts AS (
    SELECT
        deal_id,
        count(*)::integer AS old_active_stakeholders
    FROM deal_stakeholders
    WHERE organisation_id = :organisation_id
      AND is_deleted = false
      AND approval_status = 'APPROVED'
    GROUP BY deal_id
),
participant_counts AS (
    SELECT
        deal_id,
        count(*)::integer AS active_contact_participants
    FROM crm_deal_participants
    WHERE organisation_id = :organisation_id
      AND participant_type = 'contact'
      AND is_deleted = false
      AND approval_status = 'APPROVED'
    GROUP BY deal_id
)
SELECT
    coalesce(old_counts.deal_id, participant_counts.deal_id) AS deal_id,
    coalesce(old_counts.old_active_stakeholders, 0)::integer AS old_active_stakeholders,
    coalesce(participant_counts.active_contact_participants, 0)::integer
        AS active_contact_participants,
    (
        coalesce(old_counts.old_active_stakeholders, 0)
        - coalesce(participant_counts.active_contact_participants, 0)
    )::integer AS delta
FROM old_counts
FULL OUTER JOIN participant_counts USING (deal_id)
ORDER BY abs(
    coalesce(old_counts.old_active_stakeholders, 0)
    - coalesce(participant_counts.active_contact_participants, 0)
) DESC, deal_id
"""

UNMATCHED_STAKEHOLDERS_SQL = """
SELECT
    stakeholders.deal_id,
    stakeholders.stakeholder_id,
    stakeholders.email
FROM deal_stakeholders AS stakeholders
WHERE stakeholders.organisation_id = :organisation_id
  AND stakeholders.is_deleted = false
  AND stakeholders.approval_status = 'APPROVED'
  AND NOT EXISTS (
      SELECT 1
      FROM crm_deal_participants AS participants
      WHERE participants.organisation_id = stakeholders.organisation_id
        AND participants.deal_id = stakeholders.deal_id
        AND participants.participant_type = 'contact'
        AND participants.is_deleted = false
        AND participants.approval_status = 'APPROVED'
        AND (
            participants.participant_id = stakeholders.stakeholder_id
            OR participants.participant_id = stakeholders.source_reference_id
            OR participants.related_local_id = stakeholders.contact_id
            OR (
                stakeholders.email IS NOT NULL
                AND participants.related_email IS NOT NULL
                AND lower(btrim(participants.related_email)) = lower(btrim(stakeholders.email))
            )
        )
  )
ORDER BY stakeholders.deal_id, stakeholders.email NULLS LAST, stakeholders.stakeholder_id
"""

PARTICIPANTS_WITHOUT_CONTACT_SQL = """
SELECT
    participants.deal_id,
    participants.participant_id,
    participants.related_email,
    participants.related_local_id
FROM crm_deal_participants AS participants
LEFT JOIN crm_contacts AS contacts
  ON contacts.organisation_id = participants.organisation_id
 AND contacts.contact_id = participants.related_local_id
 AND contacts.is_deleted = false
WHERE participants.organisation_id = :organisation_id
  AND participants.participant_type = 'contact'
  AND participants.is_deleted = false
  AND participants.approval_status = 'APPROVED'
  AND (
      participants.related_local_id IS NULL
      OR contacts.contact_id IS NULL
  )
ORDER BY participants.deal_id, participants.related_email NULLS LAST, participants.participant_id
"""


@dataclass(frozen=True)
class DealCutoverCount:
    deal_id: UUID
    old_active_stakeholders: int
    active_contact_participants: int
    delta: int

    @property
    def matched(self) -> bool:
        return self.delta == 0


@dataclass(frozen=True)
class UnmatchedStakeholder:
    deal_id: UUID
    stakeholder_id: UUID
    email: str | None


@dataclass(frozen=True)
class ParticipantContactGap:
    deal_id: UUID
    participant_id: UUID
    related_email: str | None
    related_local_id: UUID | None


@dataclass(frozen=True)
class CutoverValidationReport:
    rows: list[DealCutoverCount]
    unmatched_stakeholders: list[UnmatchedStakeholder]
    participant_contact_gaps: list[ParticipantContactGap]

    @property
    def total_deals_compared(self) -> int:
        return len(self.rows)

    @property
    def mismatched_deals(self) -> int:
        return sum(1 for row in self.rows if not row.matched)

    @property
    def has_mismatches(self) -> bool:
        return self.mismatched_deals > 0

    @property
    def unmatched_stakeholders_count(self) -> int:
        return len(self.unmatched_stakeholders)

    @property
    def participant_contact_gap_count(self) -> int:
        return len(self.participant_contact_gaps)

    @property
    def has_validation_failures(self) -> bool:
        return self.unmatched_stakeholders_count > 0 or self.participant_contact_gap_count > 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Compare active deal_stakeholders rows with active contact participants "
            "for one organisation."
        ),
    )
    parser.add_argument("--organisation-id", required=True, type=UUID)
    parser.add_argument("--show-matched", action="store_true")
    parser.add_argument("--only-mismatches", action="store_true")
    parser.add_argument(
        "--fail-on-mismatch",
        action="store_true",
        help="Exit with status 1 when legacy stakeholders are uncovered or contacts are missing.",
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.show_matched == args.only_mismatches:
        raise SystemExit("Pass exactly one of --show-matched or --only-mismatches.")


def _as_uuid(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _row_from_mapping(row: dict[str, Any]) -> DealCutoverCount:
    return DealCutoverCount(
        deal_id=_as_uuid(row["deal_id"]),
        old_active_stakeholders=int(row["old_active_stakeholders"]),
        active_contact_participants=int(row["active_contact_participants"]),
        delta=int(row["delta"]),
    )


def _optional_uuid(value: Any) -> UUID | None:
    if value is None:
        return None
    return _as_uuid(value)


def _unmatched_from_mapping(row: dict[str, Any]) -> UnmatchedStakeholder:
    return UnmatchedStakeholder(
        deal_id=_as_uuid(row["deal_id"]),
        stakeholder_id=_as_uuid(row["stakeholder_id"]),
        email=row["email"],
    )


def _contact_gap_from_mapping(row: dict[str, Any]) -> ParticipantContactGap:
    return ParticipantContactGap(
        deal_id=_as_uuid(row["deal_id"]),
        participant_id=_as_uuid(row["participant_id"]),
        related_email=row["related_email"],
        related_local_id=_optional_uuid(row["related_local_id"]),
    )


async def validate_session(session: Any, organisation_id: UUID) -> CutoverValidationReport:
    params = {"organisation_id": organisation_id}
    result = await session.execute(
        text(CUTOVER_COUNT_SQL),
        params,
    )
    rows = [_row_from_mapping(dict(row)) for row in result.mappings().all()]
    unmatched_result = await session.execute(text(UNMATCHED_STAKEHOLDERS_SQL), params)
    contact_gap_result = await session.execute(text(PARTICIPANTS_WITHOUT_CONTACT_SQL), params)
    return CutoverValidationReport(
        rows=rows,
        unmatched_stakeholders=[
            _unmatched_from_mapping(dict(row)) for row in unmatched_result.mappings().all()
        ],
        participant_contact_gaps=[
            _contact_gap_from_mapping(dict(row))
            for row in contact_gap_result.mappings().all()
        ],
    )


async def validate_org(organisation_id: UUID) -> CutoverValidationReport:
    report = CutoverValidationReport(
        rows=[],
        unmatched_stakeholders=[],
        participant_contact_gaps=[],
    )
    async with acquire_org_admin_async_session(organisation_id) as session:
        report = await validate_session(session, organisation_id)
    return report


def print_report(report: CutoverValidationReport, *, show_matched: bool) -> None:
    print(f"total_deals_compared={report.total_deals_compared}")
    print(f"mismatched_deals={report.mismatched_deals}")
    print(f"unmatched_stakeholders={report.unmatched_stakeholders_count}")
    print(f"participant_contact_gaps={report.participant_contact_gap_count}")
    for row in report.rows:
        if row.matched and not show_matched:
            continue
        print(
            "deal_cutover_count "
            f"deal_id={row.deal_id} "
            f"old_active_stakeholders={row.old_active_stakeholders} "
            f"active_contact_participants={row.active_contact_participants} "
            f"delta={row.delta}"
        )
    for stakeholder in report.unmatched_stakeholders:
        print(
            "unmatched_stakeholder "
            f"deal_id={stakeholder.deal_id} "
            f"stakeholder_id={stakeholder.stakeholder_id} "
            f"email={stakeholder.email}"
        )
    for gap in report.participant_contact_gaps:
        print(
            "participant_contact_gap "
            f"deal_id={gap.deal_id} "
            f"participant_id={gap.participant_id} "
            f"related_email={gap.related_email} "
            f"related_local_id={gap.related_local_id}"
        )


def exit_code_for_report(
    report: CutoverValidationReport,
    *,
    fail_on_mismatch: bool,
) -> int:
    if fail_on_mismatch and report.has_validation_failures:
        return 1
    return 0


async def run(args: argparse.Namespace) -> int:
    validate_args(args)
    report = await validate_org(args.organisation_id)
    print_report(report, show_matched=args.show_matched)
    return exit_code_for_report(report, fail_on_mismatch=args.fail_on_mismatch)


def main() -> None:
    load_dotenv()
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
