# DevOps Production Pipeline Setup (Terraform + Private Link)

## Why this exists

This repo enforces SOC2-style private-access controls (Key Vault and Storage are reachable only via Private Link + Private DNS). That means **Terraform runs that touch Key Vault data-plane (secrets) must execute from inside the VNet**.

This document defines:

- The **production pipeline architecture** (hosted vs self-hosted)
- The **branching + promotion model** (main → dev, release/* → uat → prod)
- The **required Azure DevOps objects** (variable groups, environments, approvals)
- Troubleshooting checks when Key Vault is private

## Architecture (network + pipeline)

### Key point

- Microsoft-hosted agents (e.g. `ubuntu-latest`) **cannot reach** Private Endpoint-only resources in your VNet.
- Therefore, **Terraform plan/apply must run on a self-hosted agent in the VNet**.

### Diagram: network and execution location

```mermaid
flowchart LR
  DevOps[Azure DevOps Pipeline] --> AgentPool[Self-hosted Agent Pool<br/>inside VNet]

  subgraph VNet[Azure VNet]
    AgentPool --> DNS[Private DNS zones linked to VNet]
    AgentPool --> PEKV[Private Endpoint: Key Vault]
    AgentPool --> PESA[Private Endpoint: Storage]
    PEKV --> KV[Key Vault<br/>public access disabled]
    PESA --> SA[Storage Account<br/>public access disabled]
  end

  DevOps -. Microsoft-hosted agent .-> Blocked[Blocked by design<br/>no VNet path]
```

### Required Azure pieces

- A VNet with a **dedicated Private Endpoints subnet** (policies disabled)
- Private DNS zones linked to that VNet:
  - `privatelink.vaultcore.azure.net`
  - `privatelink.blob.core.windows.net`
- A self-hosted Azure DevOps agent VM (or scale set) inside that VNet

## Pipeline flow (promotion model)

### Branching model

- **Feature work**: `feature/<name>`
  - creates PR to `main`
  - pipeline runs **plan only** (no apply)
- **Dev deployment**: merge to `main`
  - pipeline runs **plan + apply to dev**
- **Release**: create `release/<version>`
  - pipeline runs **plan + apply to uat automatically**
  - then runs **plan + apply to prod**, gated by approval

### Diagram: CI/CD flow

```mermaid
flowchart TD
  F[feature/* push] --> P1[Plan (dev)]
  PR[PR to main] --> P2[Plan (dev)]

  M[merge/push main] --> D1[Plan (dev)] --> D2[Apply (dev)]

  R[push release/*] --> U1[Plan (uat)] --> U2[Apply (uat)] --> G[Manual Approval] --> P3[Plan (prod)] --> P4[Apply (prod)]
```

### Implementation in this repo

- Pipeline definition: [pipelines/infra-terraform.yml](../../pipelines/infra-terraform.yml)
- Shared stage template: [pipelines/templates/terraform-env-stage.yml](../../pipelines/templates/terraform-env-stage.yml)

## Azure DevOps configuration checklist

### 1) Agent pool

Create a self-hosted agent pool (example name used by default):

- `StaticAgentPool`

The agent must:

- run inside the target VNet (or have routed access)
- use VNet DNS (or otherwise resolve private DNS records)
- be allowed by NSGs/UDRs to reach Private Endpoint IPs

### 2) Variable groups

Create these variable groups:

- `terraform-dev-vars`
- `terraform-uat-vars`
- `terraform-prod-vars`

They must define the `TF_VAR_*` values consumed by the pipeline (examples: `TF_VAR_env`, `TF_VAR_subscription_id`, `TF_VAR_enable_key_vault_private_endpoints`, `TF_VAR_azuredevops_pat`, etc.).

Important:

- mark secrets (PAT, passwords, API keys) as **secret** variables
- for private Key Vault setups, ensure **`TF_VAR_azuredevops_pat` is provided** (otherwise Terraform may attempt to read it from Key Vault during provider initialization)

### 3) Environments (approvals)

Create Azure DevOps Environments:

- `terraform-dev` (optional approvals)
- `terraform-uat` (usually no approvals)
- `terraform-prod` (**approval required**) 

Set `terraform-prod` to require manual approval (and optionally checks like branch controls).

### 4) Service connections

Create one Azure service connection per environment:

- `copilotgtm-arc-ci-cd-service--dev`
- `copilotgtm-arc-ci-cd-service--uat`
- `copilotgtm-arc-ci-cd-service--prod`

Auth recommendation:

- Prefer **Workload Identity Federation (OIDC)** over client secrets.
- The pipeline scripts in this repo support both:
  - SP secret: uses `servicePrincipalKey`
  - OIDC: uses `AZURE_FEDERATED_TOKEN_FILE` if present

## How Key Vault secrets work with Private Link

Terraform resources like `azurerm_key_vault_secret` talk to the Key Vault **data plane** (`https://<vault>.vault.azure.net/...`).

When public network access is disabled and Private Endpoint is enabled:

- requests must originate from the VNet
- DNS must resolve `*.vault.azure.net` to the private endpoint IP via `privatelink.vaultcore.azure.net`

This is why running Terraform locally (outside the VNet) fails for secret updates.

## Troubleshooting checklist (when pipeline fails)

If you see errors like “only reachable from VNet”, “403 forbidden”, or timeouts:

- Verify the job is running on the **self-hosted pool**, not `ubuntu-latest`.
- On the agent VM, confirm DNS resolution:
  - `nslookup <vault-name>.vault.azure.net` returns a private IP
- Confirm Key Vault networking:
  - public access disabled
  - private endpoint approved
  - private DNS zone linked to the VNet
- Confirm RBAC:
  - pipeline identity has at least `Key Vault Secrets Officer` for secret writes
- Confirm the pipeline variable group provides:
  - `TF_VAR_azuredevops_pat` (so Terraform doesn’t try to read it from Key Vault)

---

## Change control / safety rules

- Keep private-access rollouts **one service at a time** (Key Vault → Storage → ACR → Postgres → Redis)
- For prod:
  - require approval gates
  - prefer deploying from a `release/*` branch only
