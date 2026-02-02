# CopilotGTM Infrastructure (copilot-infra)

Azure infrastructure-as-code repository for CopilotGTM, managed entirely with Terraform.

## Repository Structure

```
.pipeline/                    # Azure DevOps pipeline definitions
  azure-pipeline-dev.yml      # Dev pipeline (main, feature/* branches)
  azure-pipeline-prod.yml     # Prod pipeline (release/* branches, requires approval)
  templates/
    terraform-env-stage.yml   # Shared template for terraform plan/apply stages
terraform/azure/
  dev/                        # Dev environment (Central US)
  prod/                       # Production environment (Central US)
  local/                      # Local development environment (Central India)
  uat/                        # User acceptance testing
  nango-prod/                 # Nango integration (production)
  modules/                    # 24 reusable Terraform modules
teams-app-manifest/           # Microsoft Teams Bot manifests (dev, local, prod)
docs/                         # Technical and compliance documentation
scripts/                      # Utility and migration scripts
```

## Environments

| Aspect | Dev | Prod | Local |
|--------|-----|------|-------|
| **Branch trigger** | main, feature/* | release/* | Manual |
| **Location** | Central US | Central US | Central India |
| **DB SKU** | GP_Standard_D2s_v3 | GP_Standard_D2s_v3 | B_Standard_B1ms |
| **PgBouncer** | Enabled (port 6432) | Enabled (port 6432) | Disabled (port 5432) |
| **AKS nodes** | 3-6 (autoscale) | 3-6 (autoscale) | N/A |
| **ACR** | Basic | Standard | N/A |
| **Private Link** | Disabled | Disabled (ready to enable) | Disabled |
| **Pipeline approval** | Auto-apply | Manual approval gate | N/A |

Each environment has: `main.tf`, `variables.tf`, `outputs.tf`, `terraform.tfvars`, `backend.tf`

## Variable Flow

```
terraform.tfvars  -->  variables.tf  -->  main.tf (module calls)
                                              |
                                    modules/*/variables.tf
                                              |
                                    modules/*/main.tf (resources)
                                              |
                                    modules/*/outputs.tf
                                              |
                              main.tf exports to Azure DevOps variable group
```

Pipeline variables are exported to `app-secrets-${env}` variable group in Azure DevOps. Secrets come from `TF_VAR_*` environment variables set in `copilot-infra-secrets-${env}` variable group.

## Key Terraform Modules

**Compute:** aks, acr, app_service, devops_agent
**Data:** postgresql (with PgBouncer), redis, storage_account
**Security:** key_vault, workload_identity, service_principal, uami
**Networking:** vnet, waf, private_endpoint, public_load_balancer
**AI:** azure_openai (GPT-5, GPT-5-mini, GPT-5.2, text-embedding-3-large)
**Integration:** teams_bot, pipeline, service_connection, service_connection_acr

## Pipeline Trigger Rules

- **Dev pipeline** triggers on changes to: `terraform/azure/dev/**`, `terraform/azure/modules/**`, `.pipeline/**` on `main` or `feature/*` branches.
- **Prod pipeline** triggers on changes to: `terraform/azure/prod/**`, `terraform/azure/modules/**`, `.pipeline/**` on `release/*` branches.
- Changes to only `terraform/azure/local/**` or `terraform/azure/prod/**` will NOT trigger the dev pipeline (and vice versa) because path includes are scoped per environment.
- Shared changes (`modules/**`, `.pipeline/**`) trigger both pipelines on their respective branches.

## Naming Conventions

- Resources: `{base-name}-{env}` (e.g., `copilot-gtm-aks-dev`)
- ACR/Storage: alphanumeric only, no hyphens (e.g., `copilotgtmacr`)
- Key Vaults: `copilot-db-kv-{env}`, `copilot-master-kv-{env}`
- Service connections: `copilotgtm-arc-ci-cd-service--{env}` (OIDC, no secrets)
- Variable groups: `app-secrets-{env}`, `copilot-infra-secrets-{env}`

## Authentication

- **Pipelines to Azure:** Workload Identity Federation (OIDC) via service principal - no stored secrets
- **Pods to Azure:** Workload Identity (two identities: backend + workers)
- **Terraform state:** stored in `gtmstatestor001` storage account, `gtm-tfstate` resource group

## Common Tasks

- **Add a new Terraform module:** Create under `terraform/azure/modules/`, wire into each environment's `main.tf`
- **Change an environment variable:** Update `terraform.tfvars` for the target environment
- **Add a pipeline variable:** Add to the `azuredevops_variable_group "secrets"` block in the environment's `main.tf`
- **Add an OpenAI model:** Add entry to the `models` map in the `azure_openai` module call
- **Promote dev to prod:** Merge main into a `release/*` branch; prod pipeline requires approval

## Terraform Version

All environments use Terraform **1.9.0**, enforced in pipeline variables.
