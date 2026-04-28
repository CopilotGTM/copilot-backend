# RBAC Role Requirements

This document lists the Azure RBAC roles required for different operations in the CopilotGTM infrastructure.

---

## Key Vault Operations

### Reading Secrets (Backup)

| Role | Scope | Purpose |
|------|-------|---------|
| **Key Vault Secrets User** | Key Vault resource | Read secret values for backup |

### Writing Secrets (Restore)

| Role | Scope | Purpose |
|------|-------|---------|
| **Key Vault Secrets Officer** | Key Vault resource | Create, update, delete secrets |

### How to Assign Key Vault Roles

**Via Azure Portal:**
1. Navigate to the Key Vault (e.g., `copilot-db-kv-dev`)
2. Click **Access control (IAM)**
3. Click **+ Add** → **Add role assignment**
4. Select the required role (e.g., **Key Vault Secrets Officer**)
5. Under **Members**, select **User, group, or service principal**
6. Search for and select the user (e.g., `nikhil@copilotgtm.com`)
7. Click **Review + assign**

**Via Azure CLI:**
```bash
az role assignment create \
  --role "Key Vault Secrets Officer" \
  --assignee "user@example.com" \
  --scope "/subscriptions/<sub-id>/resourceGroups/<rg-name>/providers/Microsoft.KeyVault/vaults/<kv-name>"
```

---

## Key Vault Resources

| Environment | Key Vault Name | Resource Group |
|-------------|---------------|----------------|
| Dev | `copilot-db-kv-dev` | `copilot-gtm-app-dev` |
| Dev | `copilot-master-kv-dev` | `copilot-gtm-app-dev` |
| Prod | `copilot-db-kv-prod` | `copilot-gtm-app-prod` |
| Prod | `copilot-master-kv-prod` | `copilot-gtm-app-prod` |

---

## Migration Scripts Role Requirements

| Script | Required Role | Scope |
|--------|--------------|-------|
| `scripts/migration/backup-keyvault.ps1` | Key Vault Secrets User | Source Key Vault |
| `scripts/migration/restore-keyvault.ps1` | Key Vault Secrets Officer | Target Key Vault |
| `scripts/migration/backup-database.ps1` | PostgreSQL read access | Database |
| `scripts/migration/restore-database.ps1` | PostgreSQL write access | Database |
| `scripts/migration/backup-containers.ps1` | Storage Blob Data Reader | Storage Account |
| `scripts/migration/restore-containers.ps1` | Storage Blob Data Contributor | Storage Account |

---

## Storage Account Operations

| Role | Scope | Purpose |
|------|-------|---------|
| **Storage Blob Data Reader** | Storage Account | Read blobs for backup |
| **Storage Blob Data Contributor** | Storage Account | Read/write blobs for restore |
| **Storage Blob Delegator** | Storage Account | Generate SAS tokens |

---

## Subscription-Level Roles

These roles are typically assigned to service principals for Terraform/pipeline operations:

| Role | Purpose |
|------|---------|
| **Contributor** | Create/manage Azure resources |
| **User Access Administrator** | Manage RBAC role assignments |

---

## Workload Identity Roles (Kubernetes)

The following roles are assigned to workload identities for AKS pods:

| Identity | Role | Scope | Purpose |
|----------|------|-------|---------|
| `copilot-backend-wi-dev` | Key Vault Secrets Officer | Database Key Vault | App reads/writes DB passwords |
| `copilot-backend-wi-dev` | Key Vault Secrets Officer | Master Key Vault | App reads/writes encryption keys |
| `copilot-backend-wi-dev` | Storage Blob Data Contributor | Master Storage | App uploads/downloads files |
| `copilot-backend-wi-dev` | Storage Blob Delegator | Master Storage | App generates SAS tokens |
| `copilot-workers-wi-dev` | (Same as backend) | (Same scopes) | Workers need same access |

---

## Common Errors and Solutions

### "Caller is not authorized to perform action on resource"

**Error:**
```
ERROR: (Forbidden) Caller is not authorized to perform action on resource.
Action: 'Microsoft.KeyVault/vaults/secrets/setSecret/action'
```

**Solution:** Assign **Key Vault Secrets Officer** role to your user on the Key Vault.

### "ForbiddenByRbac"

**Error:**
```
Inner error: { "code": "ForbiddenByRbac" }
```

**Solution:** The Key Vault uses RBAC authorization. Assign the appropriate role via Access control (IAM), not Access policies.
