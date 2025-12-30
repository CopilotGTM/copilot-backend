# Azure Infrastructure Deployment Guide

This document outlines the deployment process for the Copilot GTM infrastructure.

---

## Phase 1: Bootstrap (Local Machine)

The Terraform pipeline runs on a self-hosted agent inside the VNet. We must first deploy the networking and agent from a local machine.

### Prerequisites

- Azure CLI installed and logged in (`az login`)
- Terraform installed (v1.9.0+)
- Azure DevOps PAT Token with scopes: `Agent Pools (Read & Manage)`, `Build (Read & Execute)`, `Variable Groups (Read, Create, & Manage)`

### Step 1: Set Environment Variable

```powershell
cd terraform/azure/dev
$env:TF_VAR_azuredevops_pat = "<YOUR_PAT_TOKEN>"
```

### Step 2: Initialize Terraform

```powershell
terraform init
```

### Step 3: Create Service Principal with Permissions

This creates the SP and auto-grants Microsoft Graph API permissions.

```powershell
terraform apply -target="module.service_principal" -var-file="terraform.tfvars"
```

### Step 4: Create Remaining Bootstrap Resources

```powershell
terraform apply -target="module.resource_group" -target="module.vnet" -target="module.devops_agent" -target="module.infra_pipeline" -target="azuredevops_variable_group.infra_secrets" -target="module.service_connection" -target="azuredevops_agent_pool.pool" -target="azuredevops_agent_queue.queue" -target="azuredevops_pipeline_authorization.queue_auth" -var-file="terraform.tfvars"
```

### Verification

1. **Agent Online**: Azure DevOps → Project Settings → Agent Pools → Verify agent is **Online**
2. **Variable Group Created**: Pipelines → Library → `copilot-infra-secrets-dev` exists with `TF_VAR_azuredevops_pat`
3. **Pipeline Created**: Pipelines → `copilot-app-infra-pipeline-dev` exists

---

## Phase 2: Full Deployment (Azure DevOps Pipeline)

Once the agent is online, trigger the pipeline in Azure DevOps to deploy remaining infrastructure.

The pipeline will provision:

- AKS Cluster
- WAF (Application Gateway)
- PostgreSQL and Redis (Private Link)
- Azure OpenAI (East US 2)
- Key Vaults & Storage

---

## Troubleshooting

### Agent Capacity Issues ("SkuNotAvailable")

Edit `terraform/azure/modules/devops_agent/main.tf` and change `vm_size` to an available SKU.

### 403 Forbidden Errors

If you see `Authorization_RequestDenied`, the SP permissions were not granted. Re-run Step 3:

```powershell
terraform apply -target="module.service_principal" -var-file="terraform.tfvars"
```
