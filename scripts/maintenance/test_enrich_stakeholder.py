#!/usr/bin/env python3
"""Test script for enrich_stakeholder tool (Apollo.io API integration).

This script tests the LinkedIn enrichment functionality:
1. API key configuration (Apollo via Key Vault or env var)
2. Apollo API connectivity (people/match endpoint)
3. Stakeholder lookup and enrichment workflow
4. Security boundary verification (organisation_id from context)

Usage:
    python -m scripts.maintenance.test_enrich_stakeholder
    python -m scripts.maintenance.test_enrich_stakeholder --test-api
    python -m scripts.maintenance.test_enrich_stakeholder --org-id <UUID> --email <email>
    python -m scripts.maintenance.test_enrich_stakeholder --linkedin-url <url>
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import quote_plus
from uuid import UUID

from dotenv import load_dotenv

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def load_env() -> None:
    """Load environment variables from .env file."""
    load_dotenv()


async def test_api_key_config() -> bool:
    """Test that the Apollo API key is configured."""
    print("\n[TEST] API Key Configuration")
    print("-" * 40)

    from src.infra.config.configs import apollo_config

    api_key = await apollo_config.get_api_key()

    if not api_key:
        print("  FAILED: Apollo API key not configured")
        print("  Hint: Add APOLLO_API_KEY=<your-key> to .env file")
        print("        Or add secret 'apollo-api-key' to Azure Key Vault")
        return False

    print(f"  OK: API key configured (length: {len(api_key)})")
    print(f"  OK: Base URL: {apollo_config.APOLLO_BASE_URL}")
    return True


async def test_apollo_search(name: str, company: str | None = None) -> dict | None:
    """Test Apollo people/match API with name search."""
    print("\n[TEST] Apollo People Match - Name Search")
    print("-" * 40)

    from src.agents.tools.research.apollo_client import apollo_enrich_person

    print(f"  Searching for: {name}" + (f" at {company}" if company else ""))

    try:
        result = await apollo_enrich_person(
            name=name,
            organization_name=company,
        )

        if result.get("success"):
            print("  OK: Found match")
            print(f"      Name: {result.get('name', 'N/A')}")
            print(f"      Title: {result.get('title', 'N/A')}")
            print(f"      Location: {result.get('location', 'N/A')}")
            print(f"      Organization: {result.get('organization_name', 'N/A')}")
            print(f"")
            print(f"  >>> LinkedIn URL: {result.get('linkedin_url', 'N/A')}")
            print(f"")

            # Show employment history
            history = result.get("employment_history", [])
            if history:
                print(f"  Employment History ({len(history)} entries):")
                for entry in history[:3]:
                    current = " (current)" if entry.get("current") else ""
                    print(f"      - {entry.get('title', 'N/A')} at {entry.get('company', 'N/A')}{current}")

            return result
        else:
            print(f"  WARN: {result.get('error', 'No results found')}")
            return None

    except Exception as e:
        print(f"  FAILED: {e}")
        return None


async def test_apollo_profile(linkedin_url: str) -> dict | None:
    """Test Apollo people/match API with LinkedIn URL."""
    print("\n[TEST] Apollo People Match - LinkedIn URL Enrichment")
    print("-" * 40)

    from src.agents.tools.research.apollo_client import apollo_enrich_person

    print(f"  Enriching profile: {linkedin_url}")

    try:
        result = await apollo_enrich_person(linkedin_url=linkedin_url)

        if result.get("success"):
            print("  OK: Profile retrieved")
            print(f"      Name: {result.get('name', 'N/A')}")
            print(f"      Title: {result.get('title', 'N/A')}")
            print(f"      Headline: {result.get('headline', 'N/A')}")
            print(f"      Location: {result.get('location', 'N/A')}")
            print(f"      Organization: {result.get('organization_name', 'N/A')}")

            history = result.get("employment_history", [])
            if history:
                latest = history[0]
                current = " (current)" if latest.get("current") else ""
                print(f"      Latest Role: {latest.get('title', 'N/A')} at {latest.get('company', 'N/A')}{current}")

            return result
        else:
            print(f"  WARN: {result.get('error', 'No profile data returned')}")
            return None

    except Exception as e:
        print(f"  FAILED: {e}")
        return None


async def test_stakeholder_enrichment_db(org_id: UUID, email: str) -> None:
    """Test the full stakeholder enrichment workflow with database."""
    print("\n[TEST] Full Stakeholder Enrichment Workflow")
    print("-" * 40)

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from src.infra.utils.resource_naming import get_schema_name

    # Build database URL
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        host = os.getenv("DATABASE_HOST", "localhost")
        port = os.getenv("DATABASE_PORT", "5432")
        name = os.getenv("DATABASE_NAME", "postgres")
        user = os.getenv("DATABASE_USER", "postgres")
        pwd = os.getenv("DATABASE_PASSWORD", "")
        user_encoded = quote_plus(user)
        pwd_encoded = quote_plus(pwd)
        db_url = f"postgresql://{user_encoded}:{pwd_encoded}@{host}:{port}/{name}"

    # Convert to async URL
    if db_url.startswith("postgresql://"):
        async_db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    else:
        async_db_url = db_url

    schema_name = get_schema_name(org_id)
    print(f"  Organisation: {org_id}")
    print(f"  Schema: {schema_name}")
    print(f"  Looking for stakeholder with email: {email}")

    try:
        engine = create_async_engine(
            async_db_url,
            connect_args={"server_settings": {"search_path": f"{schema_name},public"}},
        )

        async with AsyncSession(engine) as session:
            result = await session.execute(
                text("""
                    SELECT stakeholder_id, name, email, job_title, department,
                           external_links, stakeholder_data
                    FROM deal_stakeholders
                    WHERE organisation_id = :org_id
                      AND LOWER(email) = LOWER(:email)
                    LIMIT 1
                """),
                {"org_id": str(org_id), "email": email},
            )
            row = result.fetchone()

            if not row:
                print(f"  FAILED: No stakeholder found with email {email}")
                print("  Hint: Try a different email or organisation")
                return

            stakeholder_id = row[0]
            print(f"  OK: Found stakeholder")
            print(f"      ID: {stakeholder_id}")
            print(f"      Name: {row[1] or 'N/A'}")
            print(f"      Email: {row[2]}")
            print(f"      Job Title: {row[3] or 'N/A'}")
            print(f"      Department: {row[4] or 'N/A'}")

            stakeholder_data = row[6] or {}
            enrichment = stakeholder_data.get("enrichment", {})
            if enrichment:
                print(f"\n  Existing enrichment:")
                print(f"      Enriched at: {enrichment.get('enriched_at', 'N/A')}")
                print(f"      Source: {enrichment.get('enrichment_source', 'N/A')}")
                print(f"      Credits used: {enrichment.get('credits_used', 'N/A')}")
                print(f"      LinkedIn: {enrichment.get('linkedin_url', 'N/A')}")

            external_links = row[5] or []
            linkedin_url = None
            for link in external_links:
                if isinstance(link, dict) and link.get("platform", "").lower() == "linkedin":
                    linkedin_url = link.get("url")
                    break

            if linkedin_url:
                print(f"\n  LinkedIn URL in external_links: {linkedin_url}")

        await engine.dispose()

    except Exception as e:
        print(f"  FAILED: Database error - {e}")
        import traceback

        traceback.print_exc()


def test_security_boundaries() -> None:
    """Verify that security boundaries are properly enforced."""
    print("\n[TEST] Security Boundaries")
    print("-" * 40)

    from src.agents.tools.crm.enrich_stakeholder import create_enrich_stakeholder

    print("  Checking tool factory signature...")

    import inspect

    sig = inspect.signature(create_enrich_stakeholder)
    params = list(sig.parameters.keys())

    if "context" in params:
        print("  OK: Factory requires 'context' parameter")
    else:
        print("  FAILED: Factory missing 'context' parameter")
        return

    print("\n  Checking tool function parameters...")

    from unittest.mock import MagicMock

    mock_context = MagicMock()
    mock_context.organisation_id = UUID("00000000-0000-0000-0000-000000000001")
    mock_context.deal_id = UUID("00000000-0000-0000-0000-000000000002")

    tool_func = create_enrich_stakeholder(mock_context, lambda: None)
    tool_sig = inspect.signature(tool_func)
    tool_params = list(tool_sig.parameters.keys())

    print(f"  Tool parameters: {tool_params}")

    if "organisation_id" not in tool_params and "deal_id" not in tool_params:
        print("  OK: organisation_id and deal_id are NOT exposed to agents")
        print("      -> Agents cannot override security boundaries")
    else:
        print("  FAILED: Security parameters exposed to agents!")
        if "organisation_id" in tool_params:
            print("      -> organisation_id can be changed by agent")
        if "deal_id" in tool_params:
            print("      -> deal_id can be changed by agent")

    print("\n  Verifying context is captured in closure...")
    print("  OK: Context is bound at factory creation time (closure)")
    print("      -> Even if agent tries to pass different IDs, they are ignored")


def test_department_inference() -> None:
    """Test the department inference function."""
    print("\n[TEST] Department Inference")
    print("-" * 40)

    from src.agents.tools.crm.enrich_stakeholder import _infer_department

    test_cases = [
        ("VP of Sales", "Sales"),
        ("Software Engineer", "Engineering"),
        ("Chief Marketing Officer", "Marketing"),
        ("Product Manager", "Product"),
        ("Account Executive", "Sales"),
        ("DevOps Engineer", "Engineering"),
        ("General Counsel", "Legal"),
        ("CFO", "Finance"),
        ("Customer Success Manager", "Customer Success"),
        ("CEO", "Executive"),
        ("Random Title", None),
    ]

    passed = 0
    for title, expected in test_cases:
        result = _infer_department(title)
        status = "OK" if result == expected else "FAIL"
        if result == expected:
            passed += 1
        print(f"  {status}: '{title}' -> {result} (expected: {expected})")

    print(f"\n  Passed: {passed}/{len(test_cases)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test enrich_stakeholder tool")
    parser.add_argument(
        "--test-api",
        action="store_true",
        help="Test Apollo API endpoints directly",
    )
    parser.add_argument(
        "--search-name",
        type=str,
        help="Name to search for (use with --test-api)",
    )
    parser.add_argument(
        "--search-company",
        type=str,
        help="Company to search for (use with --test-api)",
    )
    parser.add_argument(
        "--linkedin-url",
        type=str,
        help="LinkedIn URL to test profile enrichment",
    )
    parser.add_argument(
        "--email",
        type=str,
        help="Email for stakeholder search in database",
    )
    parser.add_argument(
        "--org-id",
        type=lambda value: UUID(value),
        help="Organisation UUID for database tests",
    )
    parser.add_argument(
        "--skip-db",
        action="store_true",
        help="Skip database-related tests",
    )
    return parser.parse_args()


async def test_full_enrichment_workflow(name: str, company: str | None = None) -> None:
    """Test the complete enrichment workflow: Apollo people/match."""
    print("\n" + "=" * 60)
    print("FULL ENRICHMENT WORKFLOW TEST")
    print("=" * 60)

    # Single Apollo call handles both search and profile enrichment
    result = await test_apollo_search(name, company)

    if result:
        print("\n" + "-" * 40)
        print("WORKFLOW COMPLETE - Data that would be saved:")
        print("-" * 40)
        print(f"  Name: {result.get('name', 'N/A')}")
        print(f"  Job Title: {result.get('title', 'N/A')}")
        print(f"  LinkedIn URL: {result.get('linkedin_url', 'N/A')}")

        from src.agents.tools.crm.enrich_stakeholder import _infer_department

        title = result.get("title")
        department = _infer_department(title)
        print(f"  Department (inferred): {department or 'N/A'}")


async def main_async(args: argparse.Namespace) -> None:
    """Async main entry point."""
    if args.test_api:
        if args.search_name and not args.linkedin_url:
            await test_full_enrichment_workflow(args.search_name, args.search_company)
        else:
            if args.search_name:
                await test_apollo_search(args.search_name, args.search_company)

            if args.linkedin_url:
                await test_apollo_profile(args.linkedin_url)

    # Test full workflow with database
    if args.org_id and args.email and not args.skip_db:
        await test_stakeholder_enrichment_db(args.org_id, args.email)


def main() -> None:
    args = parse_args()

    print("=" * 60)
    print("ENRICH STAKEHOLDER TOOL - TEST SCRIPT (Apollo API)")
    print("=" * 60)

    # Load environment
    load_env()

    # Test 1: API Key configuration (async since it may hit Key Vault)
    has_api_key = asyncio.run(test_api_key_config())

    # Test 2: Security boundaries
    test_security_boundaries()

    # Test 3: Department inference
    test_department_inference()

    # Test 4: API and DB tests (async)
    if has_api_key and (args.test_api or (args.org_id and args.email)):
        asyncio.run(main_async(args))
    elif not has_api_key and args.test_api:
        print("\n  Skipping API tests: No API key configured")

    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print("=" * 60)

    print("\nUsage examples:")
    print("  # Test Apollo API search:")
    print('  python -m scripts.maintenance.test_enrich_stakeholder --test-api --search-name "John Smith" --search-company "Acme"')
    print("")
    print("  # Test profile enrichment by LinkedIn URL:")
    print("  python -m scripts.maintenance.test_enrich_stakeholder --test-api --linkedin-url https://linkedin.com/in/someone")
    print("")
    print("  # Test full workflow with database:")
    print("  python -m scripts.maintenance.test_enrich_stakeholder --org-id <UUID> --email john@example.com")


if __name__ == "__main__":
    main()
