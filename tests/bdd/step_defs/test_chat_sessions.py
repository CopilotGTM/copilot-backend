"""Step definitions for chat_sessions.feature.

These steps wire Gherkin scenarios to the REAL ChatSessionService
with a real in-memory database. No fakes, no mocks.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pytest_bdd import given, parsers, scenarios, then, when
from sqlalchemy.ext.asyncio import AsyncSession
from src.domain.models.organisation_tables.chat_session_models import ChatSession
from src.service.chat.chat_session_service import ChatSessionService

# Load all scenarios from the feature file
scenarios("../features/chat_sessions.feature")


# ---------------------------------------------------------------------------
# Storage for cross-step state
# ---------------------------------------------------------------------------


class World:
    """Shared state across steps within a single scenario."""

    def __init__(self):
        self.users: dict[str, dict] = {}  # email -> {user_id, org_id, org_name}
        self.services: dict[str, ChatSessionService] = {}  # email -> service
        self.sessions: dict[str, ChatSession] = {}  # label -> ChatSession
        self.current_user: str | None = None
        self.result: object = None
        self.error: str | None = None
        self.listed_sessions: list = []
        self.search_results: list = []


@pytest.fixture
def world():
    return World()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_or_create_user(world: World, email: str, org_name: str) -> dict:
    """Get or create a user, reusing org_id for same org_name."""
    if email in world.users:
        return world.users[email]

    # Find existing org_id for this org_name
    org_id = None
    for u in world.users.values():
        if u["org_name"] == org_name:
            org_id = u["org_id"]
            break

    user = {
        "user_id": uuid4(),
        "org_id": org_id or uuid4(),
        "org_name": org_name,
        "email": email,
    }
    world.users[email] = user
    return user


def _name_from_email(email: str) -> str:
    """alice@acme.com -> Alice"""
    return email.split("@")[0].capitalize()


def _get_service(world: World, email: str, db: AsyncSession) -> ChatSessionService:
    """Get or create a ChatSessionService for the user."""
    if email not in world.services:
        user = world.users[email]
        world.services[email] = ChatSessionService(
            db=db, organisation_id=user["org_id"], user_id=user["user_id"]
        )
    return world.services[email]


# ---------------------------------------------------------------------------
# GIVEN steps
# ---------------------------------------------------------------------------


@given(
    parsers.parse('a user "{email}" in organisation "{org_name}"'),
    target_fixture="world",
)
def given_user_in_org(world, email, org_name, db_session):
    _get_or_create_user(world, email, org_name)
    # Ensure service is created for this user
    _get_service(world, email, db_session)
    # Set as current user if first one (Alice)
    if world.current_user is None:
        world.current_user = email
    return world


@given(
    parsers.parse('Alice has a chat session titled "{title}"'),
)
def given_alice_has_session(world, title, db_session, event_loop):
    email = world.current_user
    service = _get_service(world, email, db_session)
    session = event_loop.run_until_complete(service.create_session(title=title))
    world.sessions[title] = session


@given(
    parsers.parse('Bob has a chat session titled "{title}"'),
)
def given_bob_has_session(world, title, db_session, event_loop):
    bob_email = [e for e in world.users if _name_from_email(e) == "Bob"][0]
    service = _get_service(world, bob_email, db_session)
    session = event_loop.run_until_complete(service.create_session(title=title))
    world.sessions[title] = session


@given(
    parsers.parse('Carol has a chat session titled "{title}"'),
)
def given_carol_has_session(world, title, db_session, event_loop):
    carol_email = [e for e in world.users if _name_from_email(e) == "Carol"][0]
    service = _get_service(world, carol_email, db_session)
    session = event_loop.run_until_complete(service.create_session(title=title))
    world.sessions[title] = session


# ---------------------------------------------------------------------------
# WHEN steps
# ---------------------------------------------------------------------------


@when(
    parsers.parse('Alice creates a chat session titled "{title}"'),
)
def when_alice_creates_session(world, title, db_session, event_loop):
    service = _get_service(world, world.current_user, db_session)
    world.error = None
    try:
        session = event_loop.run_until_complete(service.create_session(title=title))
        world.result = session
        world.sessions[title] = session
    except (ValueError, Exception) as e:
        world.error = str(e)


@when("Alice creates a chat session with an empty title")
def when_alice_creates_empty_title(world, db_session, event_loop):
    service = _get_service(world, world.current_user, db_session)
    world.error = None
    try:
        session = event_loop.run_until_complete(service.create_session(title=""))
        world.result = session
    except (ValueError, Exception) as e:
        world.error = str(e)


@when("Alice deletes the session")
def when_alice_deletes_session(world, db_session, event_loop):
    service = _get_service(world, world.current_user, db_session)
    # Get the most recently created session
    last_title = list(world.sessions.keys())[-1]
    session = world.sessions[last_title]
    world.error = None
    try:
        event_loop.run_until_complete(service.delete_session(session_id=session.session_id))
        world.result = "deleted"
    except (ValueError, Exception) as e:
        world.error = str(e)


@when("Alice tries to delete Bob's session")
def when_alice_deletes_bobs_session(world, db_session, event_loop):
    service = _get_service(world, world.current_user, db_session)
    bob_session = [s for t, s in world.sessions.items() if "Bob" in t or "bob" in t.lower()][0]
    world.error = None
    try:
        event_loop.run_until_complete(service.delete_session(session_id=bob_session.session_id))
        world.result = "deleted"
    except (ValueError, Exception) as e:
        world.error = str(e)


@when("Alice tries to delete Carol's session")
def when_alice_deletes_carols_session(world, db_session, event_loop):
    service = _get_service(world, world.current_user, db_session)
    carol_session = [s for t, s in world.sessions.items() if "Carol" in t][0]
    world.error = None
    try:
        event_loop.run_until_complete(service.delete_session(session_id=carol_session.session_id))
        world.result = "deleted"
    except (ValueError, Exception) as e:
        world.error = str(e)


@when(
    parsers.parse('Alice renames the session to "{new_title}"'),
)
def when_alice_renames_session(world, new_title, db_session, event_loop):
    service = _get_service(world, world.current_user, db_session)
    last_title = list(world.sessions.keys())[-1]
    session = world.sessions[last_title]
    world.error = None
    try:
        result = event_loop.run_until_complete(
            service.rename_session(session_id=session.session_id, title=new_title)
        )
        world.result = result
    except (ValueError, Exception) as e:
        world.error = str(e)


@when(
    parsers.parse('Alice tries to rename Bob\'s session to "{new_title}"'),
)
def when_alice_renames_bobs_session(world, new_title, db_session, event_loop):
    service = _get_service(world, world.current_user, db_session)
    bob_session = [s for t, s in world.sessions.items() if "Bob" in t][0]
    world.error = None
    try:
        event_loop.run_until_complete(
            service.rename_session(session_id=bob_session.session_id, title=new_title)
        )
        world.result = "renamed"
    except (ValueError, Exception) as e:
        world.error = str(e)


@when("Alice lists her sessions")
def when_alice_lists_sessions(world, db_session, event_loop):
    service = _get_service(world, world.current_user, db_session)
    items, _ = event_loop.run_until_complete(service.list_sessions(limit=100))
    world.listed_sessions = items


@when("Alice marks the session as favorite")
def when_alice_marks_favorite(world, db_session, event_loop):
    service = _get_service(world, world.current_user, db_session)
    last_title = list(world.sessions.keys())[-1]
    session = world.sessions[last_title]
    world.result = event_loop.run_until_complete(
        service.set_favorite(session_id=session.session_id, favorite=True)
    )


@when("Alice unmarks the session as favorite")
def when_alice_unmarks_favorite(world, db_session, event_loop):
    service = _get_service(world, world.current_user, db_session)
    last_title = list(world.sessions.keys())[-1]
    session = world.sessions[last_title]
    world.result = event_loop.run_until_complete(
        service.set_favorite(session_id=session.session_id, favorite=False)
    )


@when(
    parsers.parse('Alice searches for "{query}"'),
)
def when_alice_searches(world, query, db_session, event_loop):
    service = _get_service(world, world.current_user, db_session)
    results = event_loop.run_until_complete(service.search_sessions(q=query))
    world.search_results = results


# ---------------------------------------------------------------------------
# THEN steps
# ---------------------------------------------------------------------------


@then("the session is created successfully")
def then_session_created(world):
    assert world.error is None, f"Expected success but got error: {world.error}"
    assert world.result is not None


@then(parsers.parse('the session title is "{expected_title}"'))
def then_session_title_is(world, expected_title):
    assert world.result.title == expected_title


@then("the session title is at most 100 characters")
def then_title_at_most_100(world):
    assert len(world.result.title) <= 100


@then(parsers.parse('the session title ends with "{suffix}"'))
def then_title_ends_with(world, suffix):
    assert world.result.title.endswith(suffix)


@then("the session is deleted successfully")
def then_session_deleted(world):
    assert world.error is None, f"Expected success but got error: {world.error}"


@then(parsers.parse('the request is rejected with "{error_message}"'))
def then_request_rejected(world, error_message):
    assert world.error is not None, "Expected an error but operation succeeded"
    assert error_message in world.error, f"Expected '{error_message}' in error, got '{world.error}'"


@then("Bob's session still exists")
def then_bobs_session_exists(world, db_session, event_loop):
    bob_email = [e for e in world.users if _name_from_email(e) == "Bob"][0]
    bob_service = _get_service(world, bob_email, db_session)
    items, _ = event_loop.run_until_complete(bob_service.list_sessions(limit=100))
    assert len(items) > 0, "Bob's session should still exist"


@then(parsers.parse('Bob\'s session title is still "{expected_title}"'))
def then_bobs_session_title_unchanged(world, expected_title, db_session, event_loop):
    bob_email = [e for e in world.users if _name_from_email(e) == "Bob"][0]
    bob_service = _get_service(world, bob_email, db_session)
    items, _ = event_loop.run_until_complete(bob_service.list_sessions(limit=100))
    titles = [s.title for s in items]
    assert expected_title in titles, f"Expected '{expected_title}' in Bob's sessions, got {titles}"


@then(parsers.parse("Alice sees {count:d} sessions"))
def then_alice_sees_n_sessions(world, count):
    assert len(world.listed_sessions) == count, (
        f"Expected {count} sessions, got {len(world.listed_sessions)}"
    )


@then(parsers.parse('Alice does not see "{title}"'))
def then_alice_does_not_see(world, title):
    titles = [s.title for s in world.listed_sessions]
    assert title not in titles, f"Alice should not see '{title}' but it was in {titles}"


@then("the session is marked as favorite")
def then_session_is_favorite(world):
    props = world.result.properties or {}
    assert props.get("favorite") is True


@then("the session is not marked as favorite")
def then_session_is_not_favorite(world):
    props = world.result.properties or {}
    assert "favorite" not in props or props.get("favorite") is not True


@then(parsers.parse("Alice sees {count:d} session in results"))
def then_alice_sees_n_search_results(world, count):
    assert len(world.search_results) == count, (
        f"Expected {count} search results, got {len(world.search_results)}"
    )


@then(parsers.parse('the result contains "{title}"'))
def then_result_contains(world, title):
    titles = [s.title for s in world.search_results]
    assert title in titles, f"Expected '{title}' in results, got {titles}"
