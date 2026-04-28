# Infrastructure Deployment Guide

This document explains how infrastructure deployments work in this repository, including the branching model, pipeline triggers, and promotion flow from dev → uat → prod.

---

## Repository Structure

```
copilot-backend/
├── .azure-pipelines/
│   ├── infra-dev.yml                 # Dev infrastructure pipeline
│   ├── infra-prod.yml                # Prod infrastructure pipeline
│   ├── infra.yml                     # Legacy combined infrastructure pipeline
│   └── templates/
│       └── terraform-env-stage.yml   # Reusable Terraform stage template
├── infra/
│   ├── terraform/
│   │   ├── azure/
│   │   │   ├── dev/       # Dev environment config
│   │   │   ├── uat/       # UAT environment config
│   │   │   ├── prod/      # Production environment config
│   │   │   └── modules/   # Shared Terraform modules
│   │   ├── aws/
│   │   │   ├── dev/
│   │   │   ├── uat/
│   │   │   └── prod/
│   │   └── gcp/
│   │       ├── dev/
│   │       ├── uat/
│   │       └── prod/
│   └── docs/
```

---

## Branching Model

We use a **trunk-based development** model with release branches:

| Branch | Purpose | Deploys to |
|--------|---------|------------|
| `main` | Primary development branch | **Dev** (auto) |
| `feature/*` | New features (branch from main) | Nothing (plan only) |
| `hotfix/*` | Urgent fixes (branch from main) | Nothing (plan only) |
| `release/*` | Release candidates | **UAT** (auto) → **Prod** (with approval) |

---

## Deployment Flow

### Visual Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        DEVELOPMENT WORKFLOW                              │
└─────────────────────────────────────────────────────────────────────────┘

  feature/my-feature
         │
         │ (1) Push changes
         ▼
  ┌──────────────────┐
  │  Pipeline runs   │
  │  DEV PLAN ONLY   │  ← Validates terraform, no deployment
  └──────────────────┘
         │
         │ (2) Create PR to main
         ▼
  ┌──────────────────┐
  │  Pipeline runs   │
  │  DEV PLAN ONLY   │  ← Review plan output in PR
  └──────────────────┘
         │
         │ (3) Merge PR
         ▼
       main
         │
         │ (4) Auto-triggered
         ▼
  ┌──────────────────┐
  │  Pipeline runs   │
  │  DEV PLAN+APPLY  │  ← infra/terraform/azure/dev DEPLOYED ✅
  └──────────────────┘


┌─────────────────────────────────────────────────────────────────────────┐
│                         RELEASE WORKFLOW                                 │
└─────────────────────────────────────────────────────────────────────────┘

       main
         │
         │ (5) Create release branch
         ▼
  release/1.0.0
         │
         │ (6) Push branch
         ▼
  ┌──────────────────┐
  │  Pipeline runs   │
  │  UAT PLAN+APPLY  │  ← infra/terraform/azure/uat DEPLOYED ✅
  └──────────────────┘
         │
         │ (7) Auto-continues
         ▼
  ┌──────────────────┐
  │  APPROVAL GATE   │  ← Manual approval required in Azure DevOps
  │  (terraform-prod │
  │   environment)   │
  └──────────────────┘
         │
         │ (8) After approval
         ▼
  ┌──────────────────┐
  │  Pipeline runs   │
  │  PROD PLAN+APPLY │  ← infra/terraform/azure/prod DEPLOYED ✅
  └──────────────────┘
```

---

## Step-by-Step: How to Deploy

### Deploy to Dev (daily development)

```bash
# 1. Create feature branch
git checkout main
git pull origin main
git checkout -b feature/my-change

# 2. Make your terraform changes
# Edit files in infra/terraform/azure/dev/ or infra/terraform/azure/modules/

# 3. Push to trigger validation
git add .
git commit -m "feat: add new resource"
git push origin feature/my-change
# → Pipeline runs DEV PLAN ONLY (validates your changes)

# 4. Create PR to main
# → Pipeline runs DEV PLAN ONLY (reviewers see what will change)

# 5. Merge PR to main
# → Pipeline runs DEV PLAN + APPLY
# → infra/terraform/azure/dev is deployed ✅
```

### Deploy to UAT and Prod (releases)

```bash
# 1. Ensure main is stable and tested in dev
git checkout main
git pull origin main

# 2. Create release branch
git checkout -b release/1.0.0

# 3. Push release branch
git push origin release/1.0.0
# → Pipeline runs UAT PLAN + APPLY automatically
# → infra/terraform/azure/uat is deployed ✅

# 4. Pipeline pauses at PROD stage
# → Go to Azure DevOps → Pipelines → Environments → terraform-prod
# → Click "Approve" after UAT validation

# 5. After approval
# → Pipeline runs PROD PLAN + APPLY
# → infra/terraform/azure/prod is deployed ✅
```

---

## Pipeline Triggers Summary

| Action | Branch | Dev | UAT | Prod |
|--------|--------|-----|-----|------|
| Push | `feature/*` | Plan only | - | - |
| Push | `hotfix/*` | Plan only | - | - |
| PR | to `main` | Plan only | - | - |
| Merge/Push | `main` | **Plan + Apply** | - | - |
| Push | `release/*` | - | **Plan + Apply** | **Plan + Apply** (after approval) |

---

## Environment Configuration

Each environment has its own:

| Item | Dev | UAT | Prod |
|------|-----|-----|------|
| Terraform folder | `infra/terraform/azure/dev/` | `infra/terraform/azure/uat/` | `infra/terraform/azure/prod/` |
| Variable Group | `terraform-dev-vars` | `terraform-uat-vars` | `terraform-prod-vars` |
| Service Connection | `copilotgtm-arc-ci-cd-service--dev` | `copilotgtm-arc-ci-cd-service--uat` | `copilotgtm-arc-ci-cd-service--prod` |
| ADO Environment | `terraform-dev` | `terraform-uat` | `terraform-prod` |
| Approval Required | No | No | **Yes** |

---

## Azure DevOps Setup Requirements

### 1. Agent Pool

- **Name:** `StaticAgentPool`
- **Type:** Self-hosted (VM inside VNet)
- **Why:** Key Vault and Storage use Private Endpoints; agents must be inside VNet to access them
- **Provisioning:** The agent VM is created automatically by Terraform via the `devops_agent` module

### 2. Self-Hosted Agent VM (Created by Terraform)

The `devops_agent` module in `infra/terraform/azure/modules/devops_agent/` creates:

- A Linux VM (Ubuntu 22.04) in the private subnet
- Pre-installed tools: Docker, Terraform, kubectl, Azure CLI, Helm, Node.js
- Azure DevOps agent auto-registered to `StaticAgentPool`

```
┌────────────────────────────────────────────────────────────────────┐
│                          VNet (10.0.0.0/16)                        │
├────────────────────────────────────────────────────────────────────┤
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  Private Subnet (10.0.2.0/24)                               │   │
│  │  ┌──────────────────────┐  ┌──────────────────────┐        │   │
│  │  │  DevOps Agent VM     │  │  AKS Nodes           │        │   │
│  │  │  (gtm-devops-agent)  │  │                      │        │   │
│  │  └──────────────────────┘  └──────────────────────┘        │   │
│  └─────────────────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │  Private Endpoints Subnet (10.0.4.0/24)                     │   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │   │
│  │  │ Key Vault PE │  │ Storage PE   │  │ ACR PE       │       │   │
│  │  └──────────────┘  └──────────────┘  └──────────────┘       │   │
│  └─────────────────────────────────────────────────────────────┘   │
└────────────────────────────────────────────────────────────────────┘
                              │
                              │ Private DNS Resolution
                              ▼
         privatelink.vaultcore.azure.net → Private IP
         privatelink.blob.core.windows.net → Private IP
```

### 3. Variable Groups

Create these in **Pipelines → Library → Variable Groups**:

- `terraform-dev-vars`
- `terraform-uat-vars`
- `terraform-prod-vars`

**⚠️ IMPORTANT: Only add SECRETS to Variable Groups!**

Non-sensitive values stay in `terraform.tfvars` (committed in repo).

#### Required Secrets (mark each as 🔒 secret)

| Variable Name | Description |
|---------------|-------------|
| `TF_VAR_db_password` | PostgreSQL database password |
| `TF_VAR_azuredevops_pat` | Azure DevOps Personal Access Token |
| `TF_VAR_admin_password` | DevOps agent VM admin password |
| `TF_VAR_azure_openai_key` | Azure OpenAI API key |
| `TF_VAR_supabase_anon_key` | Supabase anonymous key |
| `TF_VAR_supabase_service_role_key` | Supabase service role key |
| `TF_VAR_supabase_jwt_secret` | Supabase JWT secret |
| `TF_VAR_resend_api_key` | Resend email API key |
| `TF_VAR_nango_secret` | Nango integration secret |

#### How Terraform Reads Variables

```
┌─────────────────────────────────────────────────────────────────────┐
│  Terraform automatically reads variables from:                       │
│                                                                      │
│  1. terraform.tfvars file (in repo)                                 │
│     → Non-sensitive config: location, sku, node counts, etc.        │
│                                                                      │
│  2. TF_VAR_* environment variables (from Variable Group)            │
│     → Secrets: passwords, API keys, tokens                          │
│     → Azure DevOps exports Variable Group vars as env vars          │
│     → Terraform picks up TF_VAR_* automatically!                    │
└─────────────────────────────────────────────────────────────────────┘
```

### 4. Environments

Create these in **Pipelines → Environments**:

- `terraform-dev` (no approvals)
- `terraform-uat` (no approvals)
- `terraform-prod` (**add approval gate**)

To add approval on `terraform-prod`:

1. Go to Environments → terraform-prod
2. Click "..." → Approvals and checks
3. Add "Approvals" check
4. Add approvers (yourself, team lead, etc.)

### 5. Service Connections

Create these in **Project Settings → Service connections**:

- `copilotgtm-arc-ci-cd-service--dev`
- `copilotgtm-arc-ci-cd-service--uat`
- `copilotgtm-arc-ci-cd-service--prod`

Each should be an Azure Resource Manager connection with appropriate subscription access.

---

## Rollback Procedure

If a deployment causes issues:

### Option 1: Revert commit (recommended)

```bash
# Find the bad commit
git log --oneline

# Revert it
git revert <commit-hash>
git push origin main  # or release/x.x.x
# → Pipeline redeploys with previous state
```

### Option 2: Manual terraform apply

```bash
# SSH to self-hosted agent or VPN into VNet
cd infra/terraform/azure/<env>
terraform init
terraform plan -var-file="terraform.tfvars"
terraform apply
```

---

## Troubleshooting

### Pipeline fails with "Key Vault access denied"

- **Cause:** Agent is not inside VNet, cannot reach private endpoint
- **Fix:** Ensure pipeline uses `StaticAgentPool` (self-hosted agent in VNet)

### Pipeline fails with "Variable not found"

- **Cause:** Variable group missing or variable not defined
- **Fix:** Check `terraform-<env>-vars` variable group in Azure DevOps Library

### Prod deployment not starting

- **Cause:** Waiting for approval
- **Fix:** Go to Azure DevOps → Pipelines → click the run → approve the `terraform-prod` environment

### UAT/Prod stages skipped

- **Cause:** Not on a `release/*` branch
- **Fix:** Create and push a `release/x.x.x` branch

---

## Security Notes

1. **Never commit secrets** to terraform.tfvars - use variable groups
2. **Prod approvals are mandatory** - enforced by environment gate
3. **State files are remote** - stored in Azure Storage with RBAC
4. **Private endpoints** - Key Vault/Storage only accessible from VNet
5. **Agent pool** - must be self-hosted inside VNet for private access

---

## Deploying to Production (Any Region)

### Example: Deploying to Nigeria or any new region

#### Step 1: Create terraform.tfvars for prod

Copy from dev and update non-sensitive values:

```hcl
# infra/terraform/azure/prod/terraform.tfvars
env                 = "prod"
location            = "South Africa North"  # Closest to Nigeria
resource_group_name = "copilot-gtm-app"
# ... other config values (not secrets!)
```

#### Step 2: Create Variable Group `terraform-prod-vars`

In Azure DevOps → Pipelines → Library:

1. Click **+ Variable group**
2. Name: `terraform-prod-vars`
3. Add each secret (click 🔒 padlock to mark as secret):

| Name | Value | Secret? |
|------|-------|---------|
| `TF_VAR_db_password` | (prod password) | 🔒 Yes |
| `TF_VAR_azuredevops_pat` | (PAT token) | 🔒 Yes |
| `TF_VAR_admin_password` | (VM password) | 🔒 Yes |
| `TF_VAR_azure_openai_key` | (API key) | 🔒 Yes |
| `TF_VAR_supabase_anon_key` | (key) | 🔒 Yes |
| `TF_VAR_supabase_service_role_key` | (key) | 🔒 Yes |
| `TF_VAR_supabase_jwt_secret` | (secret) | 🔒 Yes |
| `TF_VAR_resend_api_key` | (API key) | 🔒 Yes |
| `TF_VAR_nango_secret` | (secret) | 🔒 Yes |

#### Step 3: (Optional) Link to Azure Key Vault

For **maximum security**, link your Variable Group to Azure Key Vault:

1. In Variable Group, click **Link secrets from an Azure key vault**
2. Select your Key Vault
3. Map secrets: `db-password` → `TF_VAR_db_password`

```
┌─────────────────┐      ┌──────────────────┐      ┌────────────┐
│  Azure Key Vault│ ───► │  Variable Group  │ ───► │  Pipeline  │
│  (prod-secrets) │      │  (linked to KV)  │      │            │
│                 │      │                  │      │            │
│  db-password    │      │ TF_VAR_db_password│     │ terraform  │
│  api-key        │      │ TF_VAR_api_key   │      │ plan/apply │
└─────────────────┘      └──────────────────┘      └────────────┘
```

#### Step 4: Create Service Connection

In Project Settings → Service connections:

1. New service connection → Azure Resource Manager
2. Name: `copilotgtm-arc-ci-cd-service--prod`
3. Select production subscription
4. Grant access to all pipelines

#### Step 5: Deploy

```bash
# Create release branch
git checkout main
git checkout -b release/1.0.0
git push origin release/1.0.0

# Pipeline will:
# 1. Deploy to UAT (auto)
# 2. Wait for approval
# 3. Deploy to Prod (after approval)
```

---

## DevOps Agent Module Reference

### Module Location

`infra/terraform/azure/modules/devops_agent/`

### What it Creates

| Resource | Description |
|----------|-------------|
| Linux VM | Ubuntu 22.04 LTS in private subnet |
| Network Interface | Private IP only (no public IP by default) |
| NSG | Allows outbound HTTPS/HTTP for agent communication |
| Cloud-init | Auto-installs DevOps agent + tools |

### Pre-installed Tools

- Azure DevOps Agent (latest version)
- Docker CE
- Terraform 1.9.0
- kubectl
- Azure CLI
- Helm 3
- Node.js 20.x
- Git

### Module Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `vm_name` | Name of the VM | Required |
| `vm_size` | VM size | `Standard_B2s` |
| `subnet_id` | Subnet to place VM in | Required |
| `azuredevops_url` | Azure DevOps org URL | Required |
| `azuredevops_pat` | PAT for agent registration | Required |
| `agent_pool` | Pool name to register to | Required |
| `agent_name` | Agent name in pool | Required |
| `enable_public_ip` | Assign public IP | `false` |
| `install_docker` | Install Docker | `true` |
| `install_terraform` | Install Terraform | `true` |
| `install_kubectl` | Install kubectl | `true` |
| `install_azure_cli` | Install Azure CLI | `true` |

### Example Usage

```hcl
module "devops_agent" {
  source              = "../modules/devops_agent"
  resource_group_name = module.resource_group.resource_group_name
  location            = var.location
  vm_name             = "gtm-devops-agent-dev"
  vm_size             = "Standard_B2s"
  admin_username      = "azureuser"
  admin_password      = var.admin_password
  subnet_id           = module.vnet.private_subnet_ids[0]
  azuredevops_url     = "https://dev.azure.com/copilotgtm"
  azuredevops_pat     = var.azuredevops_pat
  agent_pool          = "StaticAgentPool"
  agent_name          = "gtm-devops-agent-01"
  
  tags = {
    environment = "dev"
    purpose     = "azure-devops-agent"
  }
}
```

### First-Time Setup (Bootstrap Problem)

⚠️ **Important**: The DevOps agent VM is created by Terraform, but Terraform needs an agent to run! This creates a bootstrap problem.

**Solution - One-time manual bootstrap:**

1. **First run**: Apply Terraform locally or from Azure Cloud Shell (which has network access):

   ```bash
   cd infra/terraform/azure/dev
   terraform init
   terraform apply
   ```

2. **Wait for agent**: After apply completes, wait 5-10 minutes for cloud-init to:
   - Install all tools
   - Download Azure DevOps agent
   - Register to `StaticAgentPool`

3. **Verify agent**: In Azure DevOps → Project Settings → Agent Pools → StaticAgentPool
   - Agent should appear as "Online"

4. **Future runs**: Pipeline can now use the self-hosted agent for all subsequent runs

### Troubleshooting Agent Issues

**Agent not appearing in pool:**

```bash
# SSH into the VM (if you have access)
ssh azureuser@<private-ip>

# Check cloud-init logs
sudo cat /var/log/devops-agent-setup.log

# Check agent status
cd /opt/azure-devops-agent
./svc.sh status
```

**Agent offline:**

```bash
# Restart the agent service
cd /opt/azure-devops-agent
sudo ./svc.sh stop
sudo ./svc.sh start
```

**Re-register agent:**

```bash
cd /opt/azure-devops-agent
./config.sh remove --auth pat --token <PAT>
./config.sh --unattended --url https://dev.azure.com/copilotgtm --auth pat --token <PAT> --pool StaticAgentPool --agent <name> --replace
sudo ./svc.sh install azureuser
sudo ./svc.sh start
```
