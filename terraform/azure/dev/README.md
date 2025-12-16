# Azure Terraform – Dev environment

This folder contains the **core Azure foundation** stack for the dev environment.

## Deploy (two-step)

1) Core:

```sh
cd terraform/azure/dev
terraform init
terraform plan -var-file="terraform.tfvars" -out=tfplan
terraform apply tfplan
```

2) AKS add-ons:

```sh
cd terraform/azure/dev/k8s
terraform init
terraform plan -var-file="terraform.tfvars" -out=tfplan
terraform apply tfplan
```

## Dev-specific notes

- Core state key is `azure/dev/terraform.tfstate` (configured in `provider.tf`).
- The k8s stack reads remote state from `azure/dev/terraform.tfstate` and writes its own state using the backend key in `k8s/provider.tf`.
- DNS naming in dev typically uses hosts like `devapp.<zone>`, `devadmin.<zone>`, `devapi.<zone>` (see `terraform.tfvars`).

## Secrets

- Do **not** commit real values in `terraform.tfvars` (DevOps PATs, API keys, passwords).
- Prefer passing secrets via CI (`TF_VAR_*`) or Azure DevOps secret variables/variable groups.
