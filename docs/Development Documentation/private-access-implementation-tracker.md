# Private Access (Audit Blockers) — Implementation Tracker

This tracker records the **module-by-module** rollout of the audit-blocker item:

- Disable public access
- Add Private Endpoints (Private Link) + Private DNS

**Rule for rollout (important):** we implement and enable **one Terraform module/service at a time** to avoid breaking the running backend.

## Status legend

- **Not started**: no Terraform support yet
- **Implemented (Terraform)**: module + env wiring exists, typically behind a toggle
- **Enabled (env)**: the toggle is ON for an environment (dev/uat/prod)
- **Applied**: Terraform has been applied successfully

## Service-by-service status

| Service | Terraform module(s) | What we did (one line) | Status (Terraform) | Status (dev) |
|---|---|---|---|---|
| **Networking prerequisite** | `terraform/azure/modules/vnet` | Added a dedicated subnet for Private Endpoints with required subnet policies disabled. | Implemented | **Applied on next `terraform apply`** |
| **Key Vault** | `terraform/azure/modules/key_vault` | Added `public_network_access_enabled` + Private Endpoint + DNS zone group support. Private Link **mandatory** in module. | Implemented | **Applied** |
| **Storage Account** | `terraform/azure/modules/storage_account` | Added `public_network_access_enabled` + optional Private Endpoint + DNS zone group (blob subresource). | Implemented | Toggle ON, pending apply |
| ACR | `terraform/azure/modules/acr` | Disable public access + add PE + private DNS (`privatelink.azurecr.io`). | Not started | Not started |
| Postgres Flexible Server | `terraform/azure/modules/postgresql` | Disable public access + remove allow-all firewall + add PE + private DNS (`privatelink.postgres.database.azure.com`). | Not started | Not started |
| Redis Cache | `terraform/azure/modules/redis` | Disable public network access (where supported) + add PE + private DNS (`privatelink.redis.cache.windows.net`). | Not started | Not started |

## Implementation details (done so far)

### 1) Networking prerequisite — VNet module (IMPLEMENTED)

**Where**:

- `terraform/azure/modules/vnet`
- `terraform/azure/dev`

**Changes**:

- Added optional `private_endpoints_subnet_prefix` to create a dedicated subnet named `${vnet_name}-private-endpoints`.
- The subnet has `private_endpoint_network_policies_enabled = false` which is required by Azure for Private Endpoints.
- Dev VNet now sets `vnet_private_endpoints_subnet = "10.0.4.0/24"`.

**Accessibility**:

- This subnet is intended only for Private Endpoints; workloads (AKS/App Service) continue to run in existing private subnets.

### 2) Key Vault module (IMPLEMENTED)

**Where**:

- `terraform/azure/modules/key_vault`
- `terraform/azure/dev`

**Changes**:

- Key Vault module now supports:
  - Disabling public network access (`public_network_access_enabled = false`)
  - Creating a Private Endpoint (subresource: `vault`)
  - Attaching the Private Endpoint to an existing Key Vault Private DNS zone via a DNS zone group
- Dev environment includes a rollout toggle:
  - `enable_key_vault_private_endpoints` (default: `false`)

**Accessibility / runtime safety**:

- When the toggle is enabled, Key Vault becomes reachable via Private Link from resources inside the VNet.
- **Applied in dev** (2025-12-22): Key Vault private endpoints are now mandatory; module enforces Private Link.

### 3) Storage Account module (IMPLEMENTED)

**Where**:

- `terraform/azure/modules/storage_account`
- `terraform/azure/dev`

**Changes**:

- Storage Account module now supports:
  - Disabling public network access (`public_network_access_enabled`, default `true`)
  - Creating a Private Endpoint (currently for `blob` subresource)
  - Attaching the Private Endpoint to a Private DNS zone via a DNS zone group
- Dev environment includes a rollout toggle:
  - `enable_storage_private_endpoints` (default: `true` in tfvars)
- Private DNS zone (`privatelink.blob.core.windows.net`) and VNet link created when toggle is ON.

**Accessibility / runtime safety**:

- When enabled, Storage becomes reachable only via Private Link from inside the VNet.
- Plan validated locally; pending `terraform apply`.

### 4) PostgreSQL module (UPDATED)

**Where**:

- `terraform/azure/modules/postgresql`

**Changes**:

- Made Key Vault secret storage optional (`store_password_in_key_vault`, default `false`).
- This allows Terraform to run locally (outside VNet) without hitting Key Vault data-plane 403 errors.
- The secret already exists in Key Vault; apps still read it from there.

### 5) Terraform Pipeline (NEW)

**Where**:

- `pipelines/infra-terraform.yml`

**Purpose**:

- Run `terraform plan` on PRs (validation).
- Run `terraform plan + apply` on `main` merges with manual approval.
- Uses `StaticAgentPool` (self-hosted agent inside VNet) so it can access Private Link resources.

## Next module to implement

Proceed to **ACR** (module: `terraform/azure/modules/acr`) next, then Postgres private endpoints, then Redis.
