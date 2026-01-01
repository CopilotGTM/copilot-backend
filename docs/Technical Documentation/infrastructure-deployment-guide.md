# Azure Infrastructure Deployment Guide

This document provides step-by-step instructions to deploy the Copilot GTM infrastructure from scratch.

---

## Overview

### Architecture Summary

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              AZURE DEVOPS                                    │
│  ┌─────────────────┐    ┌─────────────────────────────────────────────────┐ │
│  │  Agent Pool     │    │  Service Connection (OIDC)                      │ │
│  │  (StaticAgent)  │    │  - No secrets to rotate                         │ │
│  └────────┬────────┘    │  - Uses Workload Identity Federation            │ │
│           │             └─────────────────────────────────────────────────┘ │
└───────────┼─────────────────────────────────────────────────────────────────┘
            │
            ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           AZURE SUBSCRIPTION                                 │
│  ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐         │
│  │   VNet          │    │  Service        │    │  Resource       │         │
│  │   + Subnets     │    │  Principal      │    │  Group          │         │
│  │   + DevOps Agent│    │  (OIDC)         │    │                 │         │
│  └─────────────────┘    └─────────────────┘    └─────────────────┘         │
│                                                                              │
│  Phase 2 (via Pipeline):                                                     │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐               │
│  │   AKS   │ │   ACR   │ │ Key     │ │ Storage │ │ Postgres│               │
│  │         │ │         │ │ Vault   │ │ Account │ │ + Redis │               │
│  └─────────┘ └─────────┘ └─────────┘ └─────────┘ └─────────┘               │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Deployment Phases

| Phase | What | Where | Why |
|-------|------|-------|-----|
| **Phase 1: Bootstrap** | VNet, Agent, Service Principal, Service Connection | Local machine | Agent must exist before pipeline can run |
| **Phase 2: Full Deploy** | AKS, ACR, Databases, Key Vaults, Storage | Azure DevOps Pipeline | Self-hosted agent needed for Private Link access |

### Authentication Model (OIDC - No Secrets!)

All service connections use **Workload Identity Federation (OIDC)**:
- No client secrets to manage or rotate
- Tokens are short-lived and auto-refresh
- Microsoft's recommended approach for production

---

## Prerequisites

### Required Tools

| Tool | Version | Installation |
|------|---------|--------------|
| Azure CLI | Latest | `winget install Microsoft.AzureCLI` |
| Terraform | >= 1.9.0 | `winget install Hashicorp.Terraform` |
| Git | Latest | `winget install Git.Git` |

### Required Access

| Access | Purpose |
|--------|---------|
| **Azure Subscription** | Owner or Contributor + User Access Administrator |
| **Entra ID (Azure AD)** | Application Administrator (to create app registrations) |
| **Azure DevOps** | Project Administrator |

### Azure DevOps PAT Token

Create a Personal Access Token with these scopes:
- `Agent Pools`: Read & Manage
- `Build`: Read & Execute
- `Variable Groups`: Read, Create & Manage
- `Service Connections`: Read, Query & Manage

**To create**: Azure DevOps → User Settings (top right) → Personal Access Tokens → New Token

---

## Phase 1: Bootstrap (Local Machine)

### Step 1: Clone and Navigate

```powershell
git clone <your-repo-url>
cd copilot-infra/terraform/azure/dev
```

### Step 2: Azure Login

```powershell
# Login to Azure
az login

# Verify correct subscription
az account show --query "{name:name, id:id}" -o table

# If wrong subscription, set it:
# az account set --subscription "<subscription-id>"
```

### Step 3: Set Environment Variables

```powershell
# Set your Azure DevOps PAT token
$env:TF_VAR_azuredevops_pat = "<YOUR_PAT_TOKEN>"
```

### Step 4: Initialize Terraform

```powershell
terraform init
```

Expected output:
```
Terraform has been successfully initialized!
```

### Step 5: Create Service Principal (Requires Entra ID Admin)

This creates the app registration + service principal with:
- Contributor role on subscription
- User Access Administrator role (for RBAC assignments)
- Microsoft Graph API permissions (auto-consented)

> **Note**: The OIDC federated credential is created in Step 6 (after the service connection) because Azure DevOps generates unique issuer/subject values.

```powershell
terraform apply -target="module.service_principal" -var-file="terraform.tfvars"
```

**What gets created:**
| Resource | Purpose |
|----------|---------|
| App Registration | Identity in Entra ID |
| Service Principal | The "user account" for the app |
| RBAC Roles | Contributor + User Access Administrator |
| Graph API Consent | Auto-granted Application.ReadWrite permissions |

### Step 6: Create Bootstrap Resources + OIDC Credential

This step creates the infrastructure AND the OIDC federated credential (using values from the service connection).

```powershell
terraform apply \
  -target="module.resource_group" \
  -target="module.vnet" \
  -target="module.devops_agent" \
  -target="module.infra_pipeline" \
  -target="azuredevops_variable_group.infra_secrets" \
  -target="module.service_connection" \
  -target="azuread_application_federated_identity_credential.devops_oidc" \
  -target="azuredevops_agent_pool.pool" \
  -target="azuredevops_agent_queue.queue" \
  -target="azuredevops_pipeline_authorization.queue_auth" \
  -var-file="terraform.tfvars"
```

**What gets created:**
| Resource | Purpose |
|----------|---------|
| Resource Group | Container for all Azure resources |
| VNet + Subnets | Network with public, private, and private endpoints subnets |
| DevOps Agent VM | Self-hosted agent for pipeline execution |
| Agent Pool + Queue | Azure DevOps agent pool configuration |
| Service Connection | OIDC-based connection to Azure (no secrets!) |
| **Federated Credential** | Links service principal to service connection (enables OIDC) |
| Variable Group | Empty group for secrets (PAT added manually in Step 7) |
| Infra Pipeline | Pipeline definition for full deployment |

> **Why this order?** The federated credential needs the `issuer` and `subject` values from the service connection. Azure DevOps generates these unique values, so we must create the service connection first.

### Step 7: Add PAT to Variable Group (Manual Step)

The variable group is created by Terraform but the PAT must be added manually (not stored in Terraform state for security).

1. Go to: **Azure DevOps** → **Pipelines** → **Library**
2. Click on `copilot-infra-secrets-dev`
3. Click **+ Add**
4. Add the following variable:
   | Name | Value | Secret |
   |------|-------|--------|
   | `TF_VAR_azuredevops_pat` | `<your-pat-token>` | ✅ Yes |
5. Click **Save**

> **Important**: This PAT is required for the Terraform Azure DevOps provider to manage pipelines, variable groups, and service connections during pipeline execution.

### Step 8: Verify Bootstrap

#### 8.1 Check Agent is Online

1. Go to: **Azure DevOps** → **Project Settings** → **Agent Pools**
2. Click on `StaticAgentPool`
3. Verify agent status is **Online** (green)

> **Note**: Agent may take 5-10 minutes to come online after VM creation.

#### 8.2 Check Service Connection (OIDC)

1. Go to: **Azure DevOps** → **Project Settings** → **Service Connections**
2. Find `copilotgtm-arc-ci-cd-service--dev`
3. Verify it shows **Workload Identity Federation** (not "Service Principal")

#### 8.3 Check Variable Group

1. Go to: **Azure DevOps** → **Pipelines** → **Library**
2. Find `copilot-infra-secrets-dev`
3. Verify `TF_VAR_azuredevops_pat` exists (shown as secret) - you added this in Step 7

#### 8.4 Check Pipeline Created

1. Go to: **Azure DevOps** → **Pipelines**
2. Find `copilot-app-infra-pipeline-dev`

---

## Phase 2: Full Deployment (Azure DevOps Pipeline)

Once bootstrap is complete, the pipeline handles remaining infrastructure.

### Option A: Run Pipeline Manually

1. Go to: **Azure DevOps** → **Pipelines** → `copilot-app-infra-pipeline-dev`
2. Click **Run pipeline**
3. Select branch: `main` (or your feature branch)
4. Click **Run**

### Option B: Push to Trigger

```powershell
git add .
git commit -m "Bootstrap complete, trigger full deployment"
git push origin main
```

### What Gets Deployed (Phase 2)

| Resource | Purpose | Status |
|----------|---------|--------|
| AKS Cluster | Kubernetes for containers | Commented (enable when ready) |
| ACR | Container registry | Commented (enable when ready) |
| PostgreSQL | Application database | Commented (enable when ready) |
| Redis | Caching layer | Commented (enable when ready) |
| Key Vaults | Secrets storage | Commented (enable when ready) |
| Storage Accounts | Blob storage | Commented (enable when ready) |
| WAF | Application Gateway | Commented (enable when ready) |

> **Note**: Most Phase 2 resources are commented out in `main.tf`. Uncomment as needed.

---

## Enabling Additional Resources

### Enable ACR (Container Registry)

1. Uncomment `module "acr"` in `terraform/azure/dev/main.tf`
2. Uncomment `module "service_connection_acr"`
3. Uncomment `azuredevops_pipeline_authorization.service_connection_acr_authorization`
4. Push changes - pipeline will deploy

### Enable AKS (Kubernetes)

1. Uncomment `module "aks"` in `terraform/azure/dev/main.tf`
2. Uncomment `azurerm_role_assignment.aks_kubelet_acr_pull`
3. Push changes - pipeline will deploy

### Enable Databases

1. Uncomment `module "application_database"` (PostgreSQL)
2. Uncomment `module "redis"`
3. Push changes - pipeline will deploy

---

## Troubleshooting

### Agent Not Coming Online

**Check VM status:**
```powershell
az vm show -g "copilot-gtm-app-dev" -n "gtm-devops-agent-01-dev" --query "provisioningState" -o tsv
```

**Check cloud-init logs (SSH into VM):**
```bash
sudo cat /var/log/cloud-init-output.log | tail -50
```

**Common fixes:**
- VM SKU not available in region → Change `vm_size` in `modules/devops_agent/main.tf`
- PAT token invalid → Regenerate and re-run Step 6

### Agent Capacity Issues ("SkuNotAvailable")

Edit `terraform/azure/modules/devops_agent/main.tf` and change `vm_size`:
```hcl
vm_size = "Standard_D2as_v4"  # Try different SKU
```

Available SKUs by region:
```powershell
az vm list-skus --location centralus --size Standard_D --output table
```

### 403 Forbidden / Authorization Denied

**Cause**: OIDC federated credential not matching or Graph permissions not granted.

**Fix:**
```powershell
# Re-run as Entra ID admin
terraform apply -target="module.service_principal" -var-file="terraform.tfvars"
```

### Service Connection Shows "Service Principal" Instead of OIDC

The service connection was created with old secret-based auth. Delete and recreate:

1. Azure DevOps → Project Settings → Service Connections
2. Delete `copilotgtm-arc-ci-cd-service--dev`
3. Re-run:
```powershell
terraform apply -target="module.service_connection" -var-file="terraform.tfvars"
```

### Terraform State Issues

If state is corrupted or out of sync:
```powershell
# Import existing resource
terraform import 'module.resource_group.azurerm_resource_group.this' '/subscriptions/<sub-id>/resourceGroups/copilot-gtm-app-dev'

# Or remove from state (use carefully!)
terraform state rm 'module.service_connection.azuredevops_serviceendpoint_azurerm.oidc[0]'
```

---

## Quick Reference Commands

### Full Bootstrap (Copy-Paste)

```powershell
cd terraform/azure/dev
$env:TF_VAR_azuredevops_pat = "<YOUR_PAT_TOKEN>"
terraform init
terraform apply -target="module.service_principal" -var-file="terraform.tfvars"
terraform apply -target="module.resource_group" -target="module.vnet" -target="module.devops_agent" -target="module.infra_pipeline" -target="azuredevops_variable_group.infra_secrets" -target="module.service_connection" -target="azuread_application_federated_identity_credential.devops_oidc" -target="azuredevops_agent_pool.pool" -target="azuredevops_agent_queue.queue" -target="azuredevops_pipeline_authorization.queue_auth" -var-file="terraform.tfvars"
```

**After Terraform completes:** Add `TF_VAR_azuredevops_pat` to the variable group manually:
1. Azure DevOps → Pipelines → Library → `copilot-infra-secrets-dev`
2. Add variable `TF_VAR_azuredevops_pat` with your PAT token (mark as secret)

### Check Deployment Status

```powershell
# Resource Group
az group show -n "copilot-gtm-app-dev" --query "properties.provisioningState" -o tsv

# VNet
az network vnet show -g "copilot-gtm-app-dev" -n "copilot-gtm-vnet-dev" --query "provisioningState" -o tsv

# Agent VM
az vm show -g "copilot-gtm-app-dev" -n "gtm-devops-agent-01-dev" --query "provisioningState" -o tsv
```

### Destroy Everything (Use Carefully!)

```powershell
terraform destroy -var-file="terraform.tfvars"
```

---

## Related Documentation

- [Identity Concepts Explained](./identity-concepts-explained.md) - Understanding SP, OIDC, Service Connections
- [DevOps Production Pipeline Setup](./devops-production-pipeline-setup.md) - Pipeline architecture and branching
- [Service Principal Permissions Guide](./service-principal-permissions-guide.md) - RBAC and Graph API permissions
