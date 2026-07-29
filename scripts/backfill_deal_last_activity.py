"""Backfill effective `crm_deals.last_activity_at` values for one organisation."""

from __future__ import annotations

import argparse
import asyncio
from uuid import UUID

from sqlmodel import col, select
from src.domain.models.organisation_tables.sync.crm.deals import CRMDeal
from src.service.crm.deals.last_activity_service import DealLastActivityService

from scripts.org_admin_session import acquire_org_admin_async_session


def build_parser() -> argparse.ArgumentParser:
    """Build the dry-run-by-default CLI contract for one organisation."""
    parser = argparse.ArgumentParser(
        description="Backfill deal last_activity_at values for one organisation."
    )
    parser.add_argument("--organisation-id", required=True, type=UUID)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    return parser


async def backfill_org(organisation_id: UUID, *, apply: bool, limit: int | None) -> int:
    """Recompute selected deal rows and commit only when explicitly requested."""
    async with acquire_org_admin_async_session(organisation_id) as session:
        stmt = (
            select(col(CRMDeal.deal_id))
            .where(
                col(CRMDeal.organisation_id) == organisation_id,
                col(CRMDeal.is_deleted).is_(False),
            )
            .order_by(col(CRMDeal.created_at).asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        deal_ids = list((await session.execute(stmt)).scalars().all())
        service = DealLastActivityService(session, organisation_id)
        # copilotgtm: this reuses the safe per-deal policy queries; replace it
        # with grouped aggregation if all-org backfills become an operational bottleneck.
        for deal_id in deal_ids:
            await service.refresh_deal(deal_id)
        if apply:
            await session.commit()
        else:
            await session.rollback()
        return len(deal_ids)


async def run(args: argparse.Namespace) -> int:
    """Execute the requested mode and report how many rows were selected."""
    count = await backfill_org(args.organisation_id, apply=args.apply, limit=args.limit)
    mode = "updated" if args.apply else "would update"
    print(f"{mode} {count} deal last-activity rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(build_parser().parse_args())))
