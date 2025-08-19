import pytest
from fastapi.testclient import TestClient
from sqlmodel import create_engine, Session, SQLModel
from sqlalchemy.pool import StaticPool
from sqlalchemy import inspect
from sqlalchemy.schema import CreateTable
from uuid import uuid4
import os

# Set environment variables to indicate test mode and provide dummy values for external services
os.environ["APP_ENV"] = "test"
os.environ.setdefault("SUPABASE_URL", "http://localhost/test")
os.environ.setdefault("SUPABASE_ANON_KEY", "test-anon-key")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-jwt-secret")
os.environ.setdefault("CLOUD_PROVIDER", "azure")
os.environ.setdefault("APPLICATION_STORAGE_NAME", "teststorage")

from src.api.fastapi_app import app  # Import your FastAPI app
from src.infra.utils.database import get_org_session # Import the dependency we want to override
from src.api.dependencies.organisation_dependencies import auth as auth_dependency

# Use an in-memory SQLite database for testing
DATABASE_URL = "sqlite:///:memory:"
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,  # ensure single shared connection so in-memory schema persists across sessions
)


def override_get_org_session():
    """Override the get_org_session dependency to use the test database."""
    with Session(engine) as session:
        yield session


# Apply the override to the FastAPI app
app.dependency_overrides[get_org_session] = override_get_org_session

def _fake_verify_session():
    # Return dict shaped like real auth.verify_session output
    return {
        "user_id": str(uuid4()),
        "organisation_id": str(uuid4()),
    }

app.dependency_overrides[auth_dependency.verify_session] = _fake_verify_session


@pytest.fixture(scope="function")
def db_session():
    """
    Pytest fixture to create a new database session for each test function.
    It creates all tables, yields the session, and then drops all tables.
    """
    # Create only tables needed for upload tests to avoid JSONB incompat (SQLite).
    # Attachment model is sufficient. Filter metadata.
    from src.domain.models.organisation_tables.chat_message_models import Attachment
    Attachment.metadata.create_all(engine, tables=[Attachment.__table__])
    with Session(engine) as session:
        yield session
    Attachment.__table__.drop(engine)


@pytest.fixture(scope="module")
def client():
    """Pytest fixture to create a TestClient for making requests to the app.
    Ensures the attachments table exists once for the module (upload tests rely on it).
    """
    from src.domain.models.organisation_tables.chat_message_models import Attachment
    # Create table if not already present (idempotent in SQLite memory engine while alive)
    Attachment.metadata.create_all(engine, tables=[Attachment.__table__])
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def test_user_ids():
    """Pytest fixture to provide consistent user/org IDs for tests."""
    return {
        "user_id": uuid4(),
        "organisation_id": uuid4(),
    }