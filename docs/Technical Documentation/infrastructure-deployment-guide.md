# Azure Infrastructure Deployment Guide

This document outlines the deployment process for the Copilot GTM infrastructure. The architecture uses a **single-stage Terraform deployment** that is bootstrapped by a self-hosted DevOps agent.

## Architecture Overview

* **Primary Region (Central US)**: Hosts the core infrastructure including AKS, PostgreSQL, Redis, and the DevOps Agent. This region is selected for optimal compute capacity and US-wide latency.
* **AI Region (East US 2)**: Hosts the Azure OpenAI Service to provide access to specific model previews (e.g., GPT-5) via Private Link.
* **Ingress**: Traffic is managed by **Azure Application Gateway (WAF v2)** integrated with AKS via the Application Gateway Ingress Controller (AGIC).
* **Security**: All deployments run on a private self-hosted agent within the VNet to ensure secure access to Private Link resources.

---

## Phase 1: Bootstrap (Local Machine)

Because the Terraform pipeline runs on a self-hosted agent inside the VNet, we must first deploy the networking and the agent itself from a local machine.

### 1. Prerequisites

* Azure CLI installed and logged in (`az login`).
* Terraform installed (v1.9.0+).
* **Azure DevOps PAT Token** with "Agent Pools (Read & Manage)" scope.

### 2. Configuration

Navigate to the environment directory (e.g., `dev`):

```bash
cd terraform/azure/dev
```

Ensure your `terraform.tfvars` is configured with the correct regions.

```hcl
location            = "centralus"
# openai_location is hardcoded to "eastus2" in main.tf
```

**Handling the Azure DevOps PAT:**
You can provide the `azuredevops_pat` in one of two ways:

**Option A: Environment Variable (Recommended)**
Set the variable in your terminal session to avoid writing secrets to disk.

```powershell
$env:TF_VAR_azuredevops_pat = "<YOUR_PAT_TOKEN>"
```

**Option B: Temporary `terraform.tfvars` Entry**
Add it to your `terraform.tfvars` file, but **ensure you do not commit this file**.

```hcl
azuredevops_pat = "<YOUR_PAT_TOKEN>"
```

### 3. Targeted Apply (The Bootstrap Command)

Run the following command to deploy **only** the components needed for the agent to come online. This skips the heavy resources (AKS, DBs) that might time out or require the agent's private network context.

```bash
# Initialize Terraform
terraform init

# Apply the Agent, Networking, Pipeline, and Service Connection
terraform apply -target="module.resource_group" -target="module.vnet" -target="module.devops_agent" -target="module.infra_pipeline" -target="azuredevops_variable_group.infra_secrets" -target="module.service_principal" -target="module.service_connection" -var-file="terraform.tfvars"
```

**Verification & Secret Configuration:**

1. **Agent Status**: Go to Azure DevOps -> Project Settings -> Agent Pools. Verify `vm-agent-dev` is **Online**.
2. **Configure Secrets**:
   * Go to **Azure DevOps -> Pipelines -> Library**.
   * Open the variable group `copilot-infra-secrets-dev`.
   * Update the `azuredevops_pat` variable with your actual PAT token (and any other required secrets).
   * **Save** the variable group.
3. **Pipeline Created**: Verify the `copilot-infra-pipeline-dev` exists in the Pipelines section.

---

## Phase 2: Full Deployment (Azure DevOps Pipeline)

Once the agent is online, the rest of the infrastructure is deployed via the Azure DevOps pipeline. This ensures that Terraform state locking and execution happen in a controlled environment.

### 1. Pipeline Execution

* Trigger the `infra-terraform` pipeline in Azure DevOps.
* **Agent Selection**: Ensure the pipeline is configured to run on the pool where your self-hosted agent is registered.

### 2. What Gets Deployed

The pipeline will run `terraform apply` (without targets), provisioning:

* **AKS Cluster**: Integrated with the pre-created VNet.
* **WAF (Application Gateway)**: Deployed into the `waf` subnet and linked to AKS via AGIC.
* **Databases**: PostgreSQL and Redis (Private Link enabled).
* **Azure OpenAI**: Deployed in **East US 2** (connected via Private Endpoint).
* **Key Vaults & Storage**: With strict network rules (accessible only from VNet).

### 3. What about the "Step 2" (K8s Folder)?

The previous "Step 2" (deploying Nginx/Cert-Manager via the `k8s` folder) has been **eliminated**.

* **Ingress**: Now handled by the Azure Application Gateway (WAF), which is deployed as a native Azure resource in the main Terraform step.
* **AGIC**: The connection between the WAF and AKS is handled by the `ingress_application_gateway` add-on enabled in the AKS module.
* **Certificates**: Managed certificates on the Application Gateway or via Azure Key Vault integration, removing the need for in-cluster Cert-Manager for ingress.

---

## Troubleshooting

### Agent Capacity Issues ("SkuNotAvailable")

If the agent fails to deploy due to regional capacity constraints:

1. Edit `terraform/azure/modules/devops_agent/main.tf`.
2. Change `vm_size` to an alternative available SKU (e.g., `Standard_D2s_v3` or `Standard_B4ms`).
3. Re-run the targeted apply command.
