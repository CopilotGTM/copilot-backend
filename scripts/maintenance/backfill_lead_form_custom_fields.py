#!/usr/bin/env python3
"""Backfill lead custom-field definitions from existing lead-form properties.

Default mode is a dry run. Use --apply to insert missing lead custom-field
definitions. Existing lead forms and existing lead property values are not
renamed or modified.
"""

# ruff: noqa: S608

from __future__ import annotations

import argparse
import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from dotenv import load_dotenv
from scripts.org_admin_session import acquire_org_admin_async_session
from sqlalchemy import select, text
from sqlmodel import col
from src.domain.models.master_tables import Organisation, OrganisationStatus
from src.infra.database.sessions import acquire_master_session


@dataclass(frozen=True)
class CandidateRow:
    field_id: str
    target_key: str
    normalized_key: str
    label: str
    data_type: str
    rank: int
    already_exists: bool


@dataclass
class OrgBackfillStats:
    candidates: int = 0
    would_create: int = 0
    created: int = 0
    skipped_existing: int = 0
    skipped_duplicate: int = 0
    skipped_json: int = 0
    skipped_invalid_key: int = 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill lead custom fields from existing lead-form property mappings.",
    )
    parser.add_argument("--organisation-id", "--org-id", type=UUID, dest="organisation_id")
    parser.add_argument("--all", action="store_true", help="Process all non-failed organisations.")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Insert missing definitions. Without this flag the script only reports changes.",
    )
    return parser


def load_organisation_ids(organisation_id: UUID | None, run_all: bool) -> list[UUID]:
    session_gen = acquire_master_session()
    session = next(session_gen)
    try:
        if run_all:
            stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.status) != OrganisationStatus.FAILED
            )
        elif organisation_id:
            stmt = select(col(Organisation.organisation_id)).where(
                col(Organisation.organisation_id) == organisation_id
            )
        else:
            return []
        return [row[0] for row in session.execute(stmt).all()]
    finally:
        with contextlib.suppress(Exception):
            session.close()
        with contextlib.suppress(Exception):
            session_gen.close()


_NORMALIZED_KEY_SQL = """
regexp_replace(
    regexp_replace(
        lower(btrim(lead_form_fields.target_key)),
        '[^a-z0-9_]+',
        '_',
        'g'
    ),
    '^_+|_+$',
    '',
    'g'
)
"""


def _candidate_cte_sql() -> str:
    return f"""  # noqa: S608 - Static SQL fragment; no user input is interpolated.
    WITH candidates AS (
        SELECT
            lead_form_fields.field_id,
            lead_form_fields.target_key,
            {_NORMALIZED_KEY_SQL} AS normalized_key,
            COALESCE(
                NULLIF(left(btrim(lead_form_fields.label), 255), ''),
                {_NORMALIZED_KEY_SQL}
            ) AS label,
            CASE lead_form_fields.data_type
                WHEN 'email' THEN 'text'
                WHEN 'phone' THEN 'text'
                WHEN 'number' THEN 'number'
                WHEN 'boolean' THEN 'boolean'
                WHEN 'date' THEN 'date'
                WHEN 'datetime' THEN 'datetime'
                ELSE 'text'
            END AS custom_data_type,
            COALESCE(lead_form_fields.required, false) AS required,
            lead_form_fields.display_order,
            COALESCE(lead_form_fields.validation, '{{}}'::jsonb) AS validation,
            lead_form_fields.created_at
        FROM lead_form_fields
        JOIN lead_forms
            ON lead_forms.form_id = lead_form_fields.form_id
            AND lead_forms.organisation_id = lead_form_fields.organisation_id
        WHERE lead_form_fields.target_type = 'property'
            AND lead_form_fields.target_key IS NOT NULL
            AND btrim(lead_form_fields.target_key) <> ''
            AND lead_form_fields.data_type <> 'json'
            AND lead_form_fields.is_deleted = false
            AND lead_forms.is_deleted = false
    ),
    ranked AS (
        SELECT
            candidates.*,
            row_number() OVER (
                PARTITION BY candidates.normalized_key
                ORDER BY
                    candidates.display_order NULLS LAST,
                    candidates.created_at,
                    candidates.field_id
            ) AS rank
        FROM candidates
        WHERE candidates.normalized_key ~ '^[a-z][a-z0-9_]{{1,62}}$'
    )
    """


def preview_sql() -> str:
    return (  # noqa: S608 - Static SQL fragments only; no user input is interpolated.
        _candidate_cte_sql()
        + """
        SELECT
            ranked.field_id::text AS field_id,
            ranked.target_key,
            ranked.normalized_key,
            ranked.label,
            ranked.custom_data_type,
            ranked.rank,
            EXISTS (
                SELECT 1
                FROM crm_custom_field_definitions existing
                WHERE existing.entity_type = 'lead'
                    AND existing.field_source = 'internal'
                    AND existing.key = ranked.normalized_key
                    AND existing.is_deleted = false
            ) AS already_exists
        FROM ranked
        ORDER BY ranked.normalized_key, ranked.rank
        """
    )


def skipped_counts_sql() -> str:
    return f"""  # noqa: S608 - Static SQL fragment; no user input is interpolated.
        SELECT
            COUNT(*) FILTER (WHERE lead_form_fields.data_type = 'json') AS skipped_json,
            COUNT(*) FILTER (
                WHERE lead_form_fields.data_type <> 'json'
                    AND NOT ({_NORMALIZED_KEY_SQL} ~ '^[a-z][a-z0-9_]{{1,62}}$')
            ) AS skipped_invalid_key
        FROM lead_form_fields
        JOIN lead_forms
            ON lead_forms.form_id = lead_form_fields.form_id
            AND lead_forms.organisation_id = lead_form_fields.organisation_id
        WHERE lead_form_fields.target_type = 'property'
            AND lead_form_fields.target_key IS NOT NULL
            AND btrim(lead_form_fields.target_key) <> ''
            AND lead_form_fields.is_deleted = false
            AND lead_forms.is_deleted = false
    """


def insert_sql() -> str:
    return (  # noqa: S608 - Static SQL fragments only; organisation_id is bound later.
        _candidate_cte_sql()
        + """
        INSERT INTO crm_custom_field_definitions (
            organisation_id,
            entity_type,
            field_source,
            key,
            label,
            data_type,
            required,
            is_predefined,
            is_active,
            is_deleted,
            display_order,
            validation,
            created_at,
            updated_at
        )
        SELECT
            :organisation_id,
            'lead',
            'internal',
            ranked.normalized_key,
            ranked.label,
            ranked.custom_data_type,
            ranked.required,
            false,
            true,
            false,
            ranked.display_order,
            ranked.validation,
            now(),
            now()
        FROM ranked
        WHERE ranked.rank = 1
            AND NOT EXISTS (
                SELECT 1
                FROM crm_custom_field_definitions existing
                WHERE existing.entity_type = 'lead'
                    AND existing.field_source = 'internal'
                    AND existing.key = ranked.normalized_key
                    AND existing.is_deleted = false
            )
        RETURNING key
        """
    )


def _candidate_from_row(row: Any) -> CandidateRow:
    mapping = row._mapping
    return CandidateRow(
        field_id=str(mapping["field_id"]),
        target_key=str(mapping["target_key"]),
        normalized_key=str(mapping["normalized_key"]),
        label=str(mapping["label"]),
        data_type=str(mapping["custom_data_type"]),
        rank=int(mapping["rank"]),
        already_exists=bool(mapping["already_exists"]),
    )


async def backfill_organisation(organisation_id: UUID, *, apply: bool) -> OrgBackfillStats:
    async with acquire_org_admin_async_session(organisation_id) as session:
        preview_result = await session.execute(text(preview_sql()))
        candidates = [_candidate_from_row(row) for row in preview_result.all()]

        skipped_result = await session.execute(text(skipped_counts_sql()))
        skipped = skipped_result.one()._mapping

        stats = OrgBackfillStats(
            candidates=len(candidates),
            skipped_json=int(skipped["skipped_json"] or 0),
            skipped_invalid_key=int(skipped["skipped_invalid_key"] or 0),
        )
        selected = [
            candidate
            for candidate in candidates
            if candidate.rank == 1 and not candidate.already_exists
        ]
        stats.would_create = len(selected)
        stats.skipped_existing = sum(
            1 for candidate in candidates if candidate.rank == 1 and candidate.already_exists
        )
        stats.skipped_duplicate = sum(1 for candidate in candidates if candidate.rank > 1)

        print(f"\n[ORG] {organisation_id}")
        for candidate in selected:
            print(
                "  + "
                f"{candidate.normalized_key} ({candidate.data_type}) "
                f"from target_key={candidate.target_key!r} label={candidate.label!r}"
            )
        if stats.skipped_duplicate:
            print(f"  - skipped_duplicate_candidates={stats.skipped_duplicate}")
        if stats.skipped_existing:
            print(f"  - skipped_existing={stats.skipped_existing}")
        if stats.skipped_json:
            print(f"  - skipped_json={stats.skipped_json}")
        if stats.skipped_invalid_key:
            print(f"  - skipped_invalid_key={stats.skipped_invalid_key}")

        if not apply:
            await session.rollback()
            return stats

        insert_result = await session.execute(
            text(insert_sql()),
            {"organisation_id": organisation_id},
        )
        stats.created = len(insert_result.fetchall())
        await session.commit()
        return stats


async def run_backfill(organisation_ids: list[UUID], *, apply: bool) -> None:
    totals = OrgBackfillStats()
    for organisation_id in organisation_ids:
        stats = await backfill_organisation(organisation_id, apply=apply)
        totals.candidates += stats.candidates
        totals.would_create += stats.would_create
        totals.created += stats.created
        totals.skipped_existing += stats.skipped_existing
        totals.skipped_duplicate += stats.skipped_duplicate
        totals.skipped_json += stats.skipped_json
        totals.skipped_invalid_key += stats.skipped_invalid_key

    mode = "APPLY" if apply else "DRY RUN"
    print(
        f"\n[SUMMARY:{mode}] "
        f"orgs={len(organisation_ids)} candidates={totals.candidates} "
        f"would_create={totals.would_create} created={totals.created} "
        f"skipped_existing={totals.skipped_existing} "
        f"skipped_duplicate={totals.skipped_duplicate} "
        f"skipped_json={totals.skipped_json} "
        f"skipped_invalid_key={totals.skipped_invalid_key}"
    )


def main() -> None:
    load_dotenv()
    args = build_parser().parse_args()
    if not args.all and not args.organisation_id:
        print("No organisations specified. Use --all or --organisation-id.")
        return

    organisation_ids = load_organisation_ids(args.organisation_id, args.all)
    if not organisation_ids:
        print("No organisations found.")
        return

    asyncio.run(run_backfill(organisation_ids, apply=args.apply))


if __name__ == "__main__":
    main()
