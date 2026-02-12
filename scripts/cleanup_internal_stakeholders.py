#!/usr/bin/env python3
"""Delete stakeholders whose email domain matches an internal domain.

Scans all org_* schemas and soft-deletes (is_deleted=True) any deal_stakeholders
row with an email ending in the specified domain (default: copilotgtm.com).

Usage:
    uv run python -m scripts.cleanup_internal_stakeholders --dry-run
    uv run python -m scripts.cleanup_internal_stakeholders
    uv run python -m scripts.cleanup_internal_stakeholders --domain acme.com
    uv run python -m scripts.cleanup_internal_stakeholders --hard-delete
"""
import argparse
import os
import sys

from sqlalchemy import create_engine, text

SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def parse_args():
    p = argparse.ArgumentParser(
        description="Soft-delete internal stakeholders across all org schemas"
    )
    p.add_argument(
        "--domain",
        default="copilotgtm.com",
        help="Email domain to match (default: copilotgtm.com)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching stakeholders without deleting",
    )
    p.add_argument(
        "--hard-delete",
        action="store_true",
        help="Permanently delete rows instead of soft-deleting",
    )
    return p.parse_args()


def get_org_schemas(engine) -> list[str]:
    """Return all org_* schema names from the database."""
    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name LIKE 'org_%' ORDER BY schema_name"
            )
        )
        return [row[0] for row in result.fetchall()]


def find_matching_stakeholders(engine, schema: str, domain: str) -> list[tuple]:
    """Find stakeholders with emails matching the given domain in a schema."""
    email_pattern = f"%@{domain}"
    with engine.connect() as conn:
        result = conn.execute(
            text(
                f"SELECT stakeholder_id, deal_id, email, name, is_deleted "  # noqa: S608
                f'FROM "{schema}".deal_stakeholders '
                f"WHERE LOWER(email) LIKE :pattern"
            ),
            {"pattern": email_pattern},
        )
        return result.fetchall()


def soft_delete_stakeholders(engine, schema: str, domain: str) -> int:
    """Soft-delete stakeholders matching the domain. Returns count affected."""
    email_pattern = f"%@{domain}"
    with engine.begin() as conn:
        result = conn.execute(
            text(
                f'UPDATE "{schema}".deal_stakeholders '  # noqa: S608
                f"SET is_deleted = TRUE, updated_at = NOW() "
                f"WHERE LOWER(email) LIKE :pattern AND is_deleted = FALSE"
            ),
            {"pattern": email_pattern},
        )
        return result.rowcount


def hard_delete_stakeholders(engine, schema: str, domain: str) -> int:
    """Permanently delete stakeholders matching the domain. Returns count affected."""
    email_pattern = f"%@{domain}"
    with engine.begin() as conn:
        result = conn.execute(
            text(
                f'DELETE FROM "{schema}".deal_stakeholders '  # noqa: S608
                f"WHERE LOWER(email) LIKE :pattern"
            ),
            {"pattern": email_pattern},
        )
        return result.rowcount


def main() -> None:
    from src.infra.utils.get_master_db_url import get_master_db_url
    from src.infra.utils.logger import get_logger

    args = parse_args()
    logger = get_logger("cleanup-internal-stakeholders")
    # Strip SQL wildcards to prevent accidental broad matches
    domain = args.domain.strip().lower().replace("%", "").replace("_", "")

    db_url = get_master_db_url()
    engine = create_engine(db_url, future=True)

    # Verify connectivity
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("[OK] Database connection successful")
    except Exception as e:
        logger.error(f"[ERROR] Database connection failed: {e}")
        sys.exit(1)

    schemas = get_org_schemas(engine)
    if not schemas:
        logger.info("No org_* schemas found.")
        return

    logger.info(f"Found {len(schemas)} org schema(s). Scanning for @{domain} stakeholders...")

    total_found = 0
    total_affected = 0

    for schema in schemas:
        rows = find_matching_stakeholders(engine, schema, domain)
        if not rows:
            continue

        total_found += len(rows)
        logger.info(f"\n  Schema: {schema}  ({len(rows)} match(es))")
        for _sid, deal_id, email, name, is_deleted in rows:
            status = "already soft-deleted" if is_deleted else "active"
            logger.info(f"    {email}  name={name}  deal={deal_id}  [{status}]")

        if args.dry_run:
            continue

        if args.hard_delete:
            count = hard_delete_stakeholders(engine, schema, domain)
            logger.info(f"    -> Hard-deleted {count} row(s)")
        else:
            count = soft_delete_stakeholders(engine, schema, domain)
            logger.info(f"    -> Soft-deleted {count} row(s)")
        total_affected += count

    logger.info(f"\n{'=' * 60}")
    logger.info(f"Total matching stakeholders found: {total_found}")
    if args.dry_run:
        logger.info("[DRY RUN] No changes made.")
    else:
        action = "hard-deleted" if args.hard_delete else "soft-deleted"
        logger.info(f"Total {action}: {total_affected}")
    logger.info("[DONE]")


if __name__ == "__main__":
    main()
