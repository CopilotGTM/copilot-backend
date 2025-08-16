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

def get_storage_account_name(organisation_id: uuid.UUID) -> str:
    """
    Generates a production-ready, globally unique, and valid Azure Storage account name.
    Format: <prefix>org<sha1_hash_of_org_id>
    - Storage account names must be 3-24 characters, lowercase letters and numbers only.
    - They must be globally unique.
    """
    # Use a hash of the organization ID for the unique part.
    org_id_str = str(organisation_id)
    hasher = hashlib.sha1(org_id_str.encode('utf-8'))
    unique_part = hasher.hexdigest()

    # The prefix for storage accounts to identify them.
    # e.g., 'gtmorg'
    prefix = f"{RESOURCE_PREFIX}org"

    # Calculate how many characters of the hash we can use.
    # Max length is 24.
    hash_len = 24 - len(prefix)
    
    # Combine prefix and the truncated hash.
    storage_name = f"{prefix}{unique_part[:hash_len]}"
    
    return storage_name.lower() # Ensure lowercase