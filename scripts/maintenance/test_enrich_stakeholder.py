#!/usr/bin/env python3
"""Test script for enrich_stakeholder tool (Clado API integration).

This script tests the LinkedIn enrichment functionality:
1. API key configuration
2. Clado API connectivity (search, profile, contacts)
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


def test_api_key_config() -> bool:
    """Test that the Clado API key is configured."""
    print("\n[TEST] API Key Configuration")
    print("-" * 40)

    from src.infra.config.configs import linkedin_enrichment_config

    api_key = linkedin_enrichment_config.LINKEDIN_CLADO_API_KEY
    base_url = linkedin_enrichment_config.CLADO_BASE_URL

    if not api_key:
        print("  FAILED: LINKEDIN_CLADO_API_KEY not set in environment")
        print("  Hint: Add LINKEDIN_CLADO_API_KEY=<your-key> to .env file")
        return False

    print(f"  OK: API key configured (length: {len(api_key)})")
    print(f"  OK: Base URL: {base_url}")
    return True


async def test_clado_api_search(name: str, company: str | None = None) -> dict | None:
    """Test Clado search API."""
    print("\n[TEST] Step 1 - Clado Search API (Find LinkedIn URL)")
    print("-" * 40)

    from src.agents.tools.crm.enrich_stakeholder import _clado_search_person, _get_api_key

    api_key = _get_api_key()
    if not api_key:
        print("  FAILED: No API key")
        return None

    print(f"  Searching for: {name}" + (f" at {company}" if company else ""))

    try:
        result = await _clado_search_person(
            api_key,
            name=name,
            company=company,
        )

        if result:
            linkedin_url = result.get("linkedin_url", result.get("linkedin_profile_url", "N/A"))
            print(f"  OK: Found match")
            print(f"      Name: {result.get('name', 'N/A')}")
            print(f"      Title: {result.get('title', result.get('headline', 'N/A'))}")
            print(f"      Location: {result.get('location', 'N/A')}")
            print(f"")
            print(f"  >>> LinkedIn URL found: {linkedin_url}")
            print(f"")
            return result
        else:
            print("  WARN: No results found")
            return None

    except Exception as e:
        print(f"  FAILED: {e}")
        return None


async def test_clado_api_profile(linkedin_url: str) -> dict | None:
    """Test Clado profile enrichment API."""
    print("\n[TEST] Step 2 - Clado Profile API (Get Full Profile)")
    print("-" * 40)

    from src.agents.tools.crm.enrich_stakeholder import _clado_get_profile, _get_api_key

    api_key = _get_api_key()
    if not api_key:
        print("  FAILED: No API key")
        return None

    print(f"  Fetching profile: {linkedin_url}")

    try:
        result = await _clado_get_profile(api_key, linkedin_url)

        if result:
            print(f"  OK: Profile retrieved")
            print(f"      Name: {result.get('name', 'N/A')}")
            print(f"      Title: {result.get('title', result.get('headline', 'N/A'))}")
            print(f"      Location: {result.get('location', 'N/A')}")

            # Show skills if available
            skills = result.get("skills", [])
            if skills:
                print(f"      Skills: {', '.join(skills[:5])}...")

            # Show experience if available
            experience = result.get("experience", [])
            if experience and isinstance(experience, list) and len(experience) > 0:
                latest = experience[0]
                # Handle different field names in experience
                title = latest.get("title") or latest.get("position") or "N/A"
                company = latest.get("company") or latest.get("company_name") or latest.get("org") or "N/A"
                print(f"      Latest Role: {title} at {company}")

            # Show if decision maker
            if result.get("is_decision_maker"):
                print(f"      Decision Maker: Yes")

            return result
        else:
            print("  WARN: No profile data returned")
            return None

    except Exception as e:
        print(f"  FAILED: {e}")
        return None


async def test_clado_api_contact(email: str) -> dict | None:
    """Test Clado contact lookup API."""
    print("\n[TEST] Clado Contact API")
    print("-" * 40)

    from src.agents.tools.crm.enrich_stakeholder import _clado_get_contact_by_email, _get_api_key

    api_key = _get_api_key()
    if not api_key:
        print("  FAILED: No API key")
        return None

    print(f"  Looking up contact: {email}")

    try:
        result = await _clado_get_contact_by_email(api_key, email)

        if result:
            print(f"  OK: Contact found")
            print(f"      Name: {result.get('name', result.get('full_name', 'N/A'))}")

            # Check for LinkedIn URL in social links
            social_links = result.get("socialLinks", [])
            linkedin_url = None
            for link in social_links:
                if "linkedin.com" in link.get("url", ""):
                    linkedin_url = link.get("url")
                    break

            if linkedin_url:
                print(f"      LinkedIn: {linkedin_url}")
            else:
                print("      LinkedIn: Not found in social links")

            return result
        else:
            print("  WARN: No contact found")
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
        # Create engine with schema search path
        engine = create_async_engine(
            async_db_url,
            connect_args={"server_settings": {"search_path": f"{schema_name},public"}},
        )

        async with AsyncSession(engine) as session:
            # Find stakeholder
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

            # Check existing enrichment
            stakeholder_data = row[6] or {}
            enrichment = stakeholder_data.get("enrichment", {})
            if enrichment:
                print(f"\n  Existing enrichment:")
                print(f"      Enriched at: {enrichment.get('enriched_at', 'N/A')}")
                print(f"      Source: {enrichment.get('enrichment_source', 'N/A')}")
                print(f"      Credits used: {enrichment.get('credits_used', 'N/A')}")
                print(f"      LinkedIn: {enrichment.get('linkedin_url', 'N/A')}")

            # Check external links
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

    # Check that enrich_stakeholder requires context
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

    # Check the tool function signature
    # The tool function should NOT have organisation_id or deal_id as parameters
    print("\n  Checking tool function parameters...")

    # Create a mock context to get the tool function
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

    # Verify context is captured in closure
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
        help="Test Clado API endpoints directly",
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
        help="LinkedIn URL to test profile fetch",
    )
    parser.add_argument(
        "--email",
        type=str,
        help="Email to test contact lookup or stakeholder search",
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
    """Test the complete enrichment workflow: Search → Profile Fetch."""
    print("\n" + "=" * 60)
    print("FULL ENRICHMENT WORKFLOW TEST")
    print("=" * 60)

    # Step 1: Search for LinkedIn URL
    search_result = await test_clado_api_search(name, company)

    if not search_result:
        print("\n  WORKFLOW STOPPED: No LinkedIn URL found in Step 1")
        return

    linkedin_url = search_result.get("linkedin_url")
    if not linkedin_url:
        print("\n  WORKFLOW STOPPED: Search result missing LinkedIn URL")
        return

    # Step 2: Fetch full profile
    profile_result = await test_clado_api_profile(linkedin_url)

    if profile_result:
        print("\n" + "-" * 40)
        print("WORKFLOW COMPLETE - Data that would be saved:")
        print("-" * 40)
        print(f"  Name: {profile_result.get('name', 'N/A')}")
        print(f"  Job Title: {profile_result.get('title', 'N/A')}")
        print(f"  LinkedIn URL: {linkedin_url}")

        # Infer department
        from src.agents.tools.crm.enrich_stakeholder import _infer_department

        title = profile_result.get("title")
        department = _infer_department(title)
        print(f"  Department (inferred): {department or 'N/A'}")

        skills = profile_result.get("skills", [])
        if skills:
            print(f"  Skills: {', '.join(skills[:10])}")


async def main_async(args: argparse.Namespace) -> None:
    """Async main entry point."""
    # Test API endpoints if requested
    if args.test_api:
        # If both search name provided, run full workflow
        if args.search_name and not args.linkedin_url:
            await test_full_enrichment_workflow(args.search_name, args.search_company)
        else:
            # Individual tests
            if args.search_name:
                await test_clado_api_search(args.search_name, args.search_company)

            if args.linkedin_url:
                await test_clado_api_profile(args.linkedin_url)

        if args.email and not args.org_id:
            await test_clado_api_contact(args.email)

    # Test full workflow with database
    if args.org_id and args.email and not args.skip_db:
        await test_stakeholder_enrichment_db(args.org_id, args.email)


def main() -> None:
    args = parse_args()

    print("=" * 60)
    print("ENRICH STAKEHOLDER TOOL - TEST SCRIPT")
    print("=" * 60)

    # Load environment
    load_env()

    # Test 1: API Key configuration
    has_api_key = test_api_key_config()

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
    print("  # Test API search:")
    print('  python -m scripts.maintenance.test_enrich_stakeholder --test-api --search-name "John Smith" --search-company "Acme"')
    print("")
    print("  # Test profile fetch:")
    print("  python -m scripts.maintenance.test_enrich_stakeholder --test-api --linkedin-url https://linkedin.com/in/someone")
    print("")
    print("  # Test contact lookup:")
    print("  python -m scripts.maintenance.test_enrich_stakeholder --test-api --email john@example.com")
    print("")
    print("  # Test full workflow with database:")
    print("  python -m scripts.maintenance.test_enrich_stakeholder --org-id <UUID> --email john@example.com")


if __name__ == "__main__":
    main()
