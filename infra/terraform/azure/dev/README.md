# Azure Terraform – Dev environment

This folder contains the **core Azure foundation** stack for the dev environment.

## Deploy (Bootstrap + Pipeline)

The deployment is now a single-stage Terraform process, bootstrapped from a local machine.

### 1. Bootstrap (Local)

Deploy the networking, agent, and pipeline definitions first:

```sh
cd infra/terraform/azure/dev
terraform init
terraform apply -target="module.resource_group" -target="module.vnet" -target="module.devops_agent" -target="module.infra_pipeline" -target="azuredevops_variable_group.infra_secrets" -target="module.service_principal" -target="module.service_connection" -target="azuredevops_agent_pool.pool" -target="azuredevops_agent_queue.queue" -target="azuredevops_pipeline_authorization.queue_auth" -var-file="terraform.tfvars"
```

### 2. Full Deploy (Azure DevOps)

Once the agent is online and secrets are configured in the `copilot-infra-secrets-dev` variable group, run the `copilot-infra-pipeline-dev` in Azure DevOps to deploy the rest of the stack (AKS, DBs, WAF, etc.).

## Dev-specific notes

- Core state key is `azure/dev/terraform.tfstate` (configured in `provider.tf`).
- DNS naming in dev typically uses hosts like `devapp.<zone>`, `devadmin.<zone>`, `devapi.<zone>` (see `terraform.tfvars`).

## Secrets

- Do **not** commit real values in `terraform.tfvars` (DevOps PATs, API keys, passwords).
- Prefer passing secrets via CI (`TF_VAR_*`) or Azure DevOps secret variables/variable groups.
