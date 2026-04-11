"""
CopilotGTM Backend Load Testing

Usage:
    cd scripts/load_testing
    locust

Then open http://localhost:8089 in your browser.
"""

import getpass
import os
import random
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv
from locust import HttpUser, between, events, task

# Load .env file from project root
project_root = Path(__file__).parent.parent.parent
env_file = project_root / ".env"
if env_file.exists():
    load_dotenv(env_file)
    print(f"Loaded environment from: {env_file}")

# Add project root to path for config import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DEFAULT_CONFIG, ENDPOINTS

# Global credentials and shared token (to avoid rate limiting)
_credentials: dict[str, str] = {}
_shared_token: dict[str, str | None] = {"access_token": None, "refresh_token": None}
_token_lock = threading.Lock()


@events.init_command_line_parser.add_listener
def add_custom_arguments(parser):
    """Add custom command line arguments."""
    parser.add_argument(
        "--email",
        type=str,
        env_var="LOAD_TEST_EMAIL",
        default="",
        help="Email for authentication (or set LOAD_TEST_EMAIL env var)",
    )
    parser.add_argument(
        "--password",
        type=str,
        env_var="LOAD_TEST_PASSWORD",
        default="",
        help="Password for authentication (or set LOAD_TEST_PASSWORD env var)",
    )


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    """Prompt for credentials and perform initial login before test starts."""
    global _credentials, _shared_token

    # Try command line args first, then environment variables, then prompt
    email = environment.parsed_options.email
    password = environment.parsed_options.password

    # Fallback to environment variables (direct check for Windows compatibility)
    if not email:
        email = os.environ.get("LOAD_TEST_EMAIL", "")
    if not password:
        password = os.environ.get("LOAD_TEST_PASSWORD", "")

    # If still not set, prompt the user
    if not email:
        email = input("Enter email: ")
    if not password:
        password = getpass.getpass("Enter password: ")

    if not email or not password:
        print("ERROR: Email and password are required!")
        sys.exit(1)

    _credentials["email"] = email
    _credentials["password"] = password
    print(f"\nCredentials set for: {email}")

    # Perform initial login to get token BEFORE spawning users
    # This avoids all users trying to login simultaneously
    import requests

    host = environment.host.rstrip("/")
    print(f"Performing initial login to {host}...")

    try:
        response = requests.post(
            f"{host}{ENDPOINTS['login']}",
            json={"email": email, "password": password},
            timeout=30,
        )
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "SUCCESS" and data.get("data"):
                tokens = data["data"].get("tokens", {})
                _shared_token["access_token"] = tokens.get("access_token")
                _shared_token["refresh_token"] = tokens.get("refresh_token")
                print("Initial login successful! Token acquired.")
            else:
                print(f"Login response unexpected: {data}")
        else:
            print(f"Initial login failed: {response.status_code} - {response.text}")
            print("Users will attempt to login individually (may hit rate limits)")
    except Exception as e:
        print(f"Initial login error: {e}")
        print("Users will attempt to login individually (may hit rate limits)")

    print("Starting load test...\n")


class AuthenticatedUser(HttpUser):
    """
    Base user that handles authentication.

    All tasks will use the authenticated session.
    Uses a shared token across all users to avoid rate limiting on login.
    """

    wait_time = between(DEFAULT_CONFIG["wait_time_min"], DEFAULT_CONFIG["wait_time_max"])
    abstract = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.deal_ids: list[str] = []  # Cache of deal IDs for subsequent requests
        self.session_ids: list[str] = []  # Cache of chat session IDs

    def on_start(self):
        """Authenticate when user starts."""
        self._get_or_create_token()
        self._fetch_initial_data()

    def _get_or_create_token(self):
        """Get shared token or create one if not exists."""
        global _shared_token

        with _token_lock:
            if _shared_token["access_token"]:
                # Use existing shared token
                self.access_token = _shared_token["access_token"]
                self.refresh_token = _shared_token["refresh_token"]
                return

        # No shared token, need to login
        self._login()

        # Share the token with other users
        with _token_lock:
            if self.access_token and not _shared_token["access_token"]:
                _shared_token["access_token"] = self.access_token
                _shared_token["refresh_token"] = self.refresh_token

    def _login(self):
        """Perform login and store tokens."""
        response = self.client.post(
            ENDPOINTS["login"],
            json={
                "email": _credentials["email"],
                "password": _credentials["password"],
            },
            name="[Auth] Login",
        )

        if response.status_code == 200:
            data = response.json()
            # Response is wrapped in GenericResponse: {"success": true, "data": {...}}
            if data.get("status") == "SUCCESS" and data.get("data"):
                tokens = data["data"].get("tokens", {})
                self.access_token = tokens.get("access_token")
                self.refresh_token = tokens.get("refresh_token")
        else:
            print(f"Login failed: {response.status_code} - {response.text}")

    def _get_headers(self) -> dict[str, str]:
        """Get auth headers for requests."""
        if self.access_token:
            return {"Authorization": f"Bearer {self.access_token}"}
        return {}

    def _refresh_auth_token(self):
        """Refresh the access token using refresh token."""
        global _shared_token

        if not self.refresh_token:
            self._get_or_create_token()
            return

        response = self.client.post(
            ENDPOINTS["refresh_token"],
            json={"refresh_token": self.refresh_token},
            name="[Auth] Refresh Token",
        )

        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "SUCCESS" and data.get("data"):
                tokens = data["data"].get("tokens", {})
                self.access_token = tokens.get("access_token")
                self.refresh_token = tokens.get("refresh_token")

                # Update shared token
                with _token_lock:
                    _shared_token["access_token"] = self.access_token
                    _shared_token["refresh_token"] = self.refresh_token
        else:
            # Fallback to full login
            self._login()

    def _fetch_initial_data(self):
        """Fetch initial data to populate caches for subsequent requests."""
        # Fetch deals to get deal IDs
        response = self.client.get(
            ENDPOINTS["deals"],
            headers=self._get_headers(),
            params={"limit": 10},
            name="[Init] Fetch Deals",
        )
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "SUCCESS" and data.get("data"):
                deals = data["data"].get("deals", [])
                self.deal_ids = [d.get("id") for d in deals if d.get("id")]

        # Fetch chat sessions to get session IDs
        response = self.client.get(
            ENDPOINTS["chat_list"],
            headers=self._get_headers(),
            name="[Init] Fetch Chats",
        )
        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "SUCCESS" and data.get("data"):
                items = data["data"].get("items", [])
                self.session_ids = [s.get("session_id") for s in items if s.get("session_id")]

    def _get_random_deal_id(self) -> str | None:
        """Get a random deal ID from cache."""
        return random.choice(self.deal_ids) if self.deal_ids else None

    def _get_random_session_id(self) -> str | None:
        """Get a random session ID from cache."""
        return random.choice(self.session_ids) if self.session_ids else None


class HealthCheckUser(HttpUser):
    """
    Simple user that only hits health endpoint.

    Useful for baseline testing.
    """

    wait_time = between(0.5, 1)
    weight = 1

    @task
    def health_check(self):
        """Check health endpoint."""
        self.client.get(ENDPOINTS["health"], name="[Health] Check")


class CRMUser(AuthenticatedUser):
    """
    User that performs CRM-related operations.

    Focuses on deals API - common read operations.
    """

    weight = 3  # Higher weight = more instances of this user type

    @task(10)
    def list_deals(self):
        """List deals - most common operation."""
        response = self.client.get(
            ENDPOINTS["deals"],
            headers=self._get_headers(),
            params={"limit": 20},
            name="[CRM] List Deals",
        )
        if response.status_code == 401:
            self._refresh_auth_token()

    @task(5)
    def list_deals_with_filters(self):
        """List deals with filters."""
        response = self.client.get(
            ENDPOINTS["deals"],
            headers=self._get_headers(),
            params={
                "limit": 10,
                "statuses": ["open"],
            },
            name="[CRM] List Deals (filtered)",
        )
        if response.status_code == 401:
            self._refresh_auth_token()

    @task(3)
    def get_deals_stats(self):
        """Get deals statistics."""
        response = self.client.get(
            ENDPOINTS["deals_stats"],
            headers=self._get_headers(),
            name="[CRM] Deals Stats",
        )
        if response.status_code == 401:
            self._refresh_auth_token()

    @task(4)
    def get_deal_tasks(self):
        """Get tasks for a specific deal."""
        deal_id = self._get_random_deal_id()
        if not deal_id:
            return

        response = self.client.get(
            ENDPOINTS["deal_tasks"].format(deal_id=deal_id),
            headers=self._get_headers(),
            name="[CRM] Deal Tasks",
        )
        if response.status_code == 401:
            self._refresh_auth_token()

    @task(3)
    def get_deal_meetings(self):
        """Get meetings for a specific deal."""
        deal_id = self._get_random_deal_id()
        if not deal_id:
            return

        response = self.client.get(
            ENDPOINTS["deal_meetings"].format(deal_id=deal_id),
            headers=self._get_headers(),
            name="[CRM] Deal Meetings",
        )
        if response.status_code == 401:
            self._refresh_auth_token()

    @task(3)
    def get_deal_stakeholders(self):
        """Get stakeholders for a specific deal."""
        deal_id = self._get_random_deal_id()
        if not deal_id:
            return

        response = self.client.get(
            ENDPOINTS["deal_stakeholders"].format(deal_id=deal_id),
            headers=self._get_headers(),
            name="[CRM] Deal Stakeholders",
        )
        if response.status_code == 401:
            self._refresh_auth_token()

    @task(2)
    def get_deal_timeline(self):
        """Get timeline for a specific deal."""
        deal_id = self._get_random_deal_id()
        if not deal_id:
            return

        response = self.client.get(
            ENDPOINTS["deal_timeline"].format(deal_id=deal_id),
            headers=self._get_headers(),
            name="[CRM] Deal Timeline",
        )
        if response.status_code == 401:
            self._refresh_auth_token()

    @task(1)
    def list_all_deals(self):
        """Fetch all deals - heavier operation."""
        response = self.client.get(
            ENDPOINTS["deals"],
            headers=self._get_headers(),
            params={"fetch_all": "true"},
            name="[CRM] List All Deals",
        )
        if response.status_code == 401:
            self._refresh_auth_token()


class ChatUser(AuthenticatedUser):
    """
    User that performs chat-related operations.

    Simulates chat interactions.
    """

    weight = 1

    @task(5)
    def list_chats(self):
        """List chat sessions."""
        response = self.client.get(
            ENDPOINTS["chat_list"],
            headers=self._get_headers(),
            name="[Chat] List Chats",
        )
        if response.status_code == 401:
            self._refresh_auth_token()

    @task(3)
    def get_chat_messages(self):
        """Get messages for a specific session."""
        session_id = self._get_random_session_id()
        if not session_id:
            return

        response = self.client.get(
            ENDPOINTS["chat_messages"],
            headers=self._get_headers(),
            params={"session_id": session_id},
            name="[Chat] Messages",
        )
        if response.status_code == 401:
            self._refresh_auth_token()


class IntegrationsUser(AuthenticatedUser):
    """
    User that checks integrations.
    """

    weight = 1

    @task(5)
    def list_available_integrations(self):
        """List available integrations."""
        response = self.client.get(
            ENDPOINTS["integrations_available"],
            headers=self._get_headers(),
            name="[Integrations] Available",
        )
        if response.status_code == 401:
            self._refresh_auth_token()


class MixedUser(AuthenticatedUser):
    """
    User that performs mixed operations.

    Simulates typical user behavior across different features.
    """

    weight = 2

    @task(5)
    def list_deals(self):
        """List deals."""
        response = self.client.get(
            ENDPOINTS["deals"],
            headers=self._get_headers(),
            params={"limit": 20},
            name="[Mixed] List Deals",
        )
        if response.status_code == 401:
            self._refresh_auth_token()

    @task(3)
    def list_chats(self):
        """List chat sessions."""
        response = self.client.get(
            ENDPOINTS["chat_list"],
            headers=self._get_headers(),
            name="[Mixed] List Chats",
        )
        if response.status_code == 401:
            self._refresh_auth_token()

    @task(2)
    def get_deals_stats(self):
        """Get deals statistics."""
        response = self.client.get(
            ENDPOINTS["deals_stats"],
            headers=self._get_headers(),
            name="[Mixed] Deals Stats",
        )
        if response.status_code == 401:
            self._refresh_auth_token()

    @task(2)
    def list_integrations(self):
        """List available integrations."""
        response = self.client.get(
            ENDPOINTS["integrations_available"],
            headers=self._get_headers(),
            name="[Mixed] Integrations",
        )
        if response.status_code == 401:
            self._refresh_auth_token()
