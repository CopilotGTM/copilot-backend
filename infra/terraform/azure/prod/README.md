# Azure Terraform – Prod environment

This folder contains the **core Azure foundation** stack for the prod environment.

## Deploy (two-step)

1) Core:

```sh
cd infra/terraform/azure/prod
terraform init
terraform plan -var-file="terraform.tfvars" -out=tfplan
terraform apply tfplan
```

2) AKS add-ons:

```sh
cd infra/terraform/azure/prod/k8s
terraform init
terraform plan -var-file="terraform.tfvars" -out=tfplan
terraform apply tfplan
```

## Prod-specific notes

- Use a **prod-specific** core state key (example: `azure/prod/terraform.tfstate`).
- Ensure the prod `k8s/provider.tf` remote-state key points to `azure/prod/terraform.tfstate`.
- Use prod DNS hostnames (typically `app.<zone>`, `admin.<zone>`, `api.<zone>`) and prod branches/pipeline variables.

## Secrets

- Keep real values out of git. Use CI secret variables / variable groups / Key Vault.
