import uuid
import hashlib
import os

# A prefix for all resources to identify them as part of this application.
# Can be configured via environment variables for different environments (e.g., "prod", "dev").
RESOURCE_PREFIX = os.getenv("RESOURCE_PREFIX", "gtm")

def get_schema_name(organisation_id: uuid.UUID) -> str:
    """Generates the database schema name for an organization."""
    return f"org_{str(organisation_id).replace('-', '_')}"

def get_role_name(organisation_id: uuid.UUID) -> str:
    """Generates the database role name for an organization."""
    return f"org_{str(organisation_id).replace('-', '_')}_role"

def get_key_vault_secret_name(organisation_id: uuid.UUID) -> str:
    """
    Generates a production-ready, unique, and descriptive name for a Key Vault secret.
    Format: <prefix>-<org_id>-db-password
    """
    # Key Vault names allow alphanumeric and dashes.
    secret_name = f"{RESOURCE_PREFIX}-{str(organisation_id)}-db-password"
    return secret_name

def get_org_container_name(organisation_id: uuid.UUID) -> str:
    """Return deterministic, storage‑compliant container name for an organisation.

    Constraints: lowercase, numbers, dash, 3-63 chars. We use prefix + short hash.
    Format: <prefix>-org-<8chars>
    """
    h = hashlib.sha1(str(organisation_id).encode("utf-8")).hexdigest()
    base = f"{RESOURCE_PREFIX}-org-{h}"
    return base.lower()