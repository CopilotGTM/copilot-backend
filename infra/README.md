# CopilotGTM Infrastructure

Terraform infrastructure for Azure. This is now an `infra/` subtree in the
`copilot-backend` monorepo; environment folders live under
`infra/terraform/azure/`.

## Docs

- Deployment guide (two-step: core + k8s): `docs/infrastructure-deployment-guide.md`
- KEDA notes: `docs/keda-autoscaling-guide.md`
- Dev → Prod promotion guide: `docs/environment-promotion-dev-to-prod.md`

## Quickstart (any environment)

```sh
az login

cd infra/terraform/azure/<env>
terraform init
terraform plan -var-file="terraform.tfvars" -out=tfplan
terraform apply tfplan

cd k8s
terraform init
terraform plan -var-file="terraform.tfvars" -out=tfplan
terraform apply tfplan
```

Notes:

- Keep real `terraform.tfvars` local/secure (it commonly contains secrets).
- See `infra/terraform/azure/dev/README.md` and `infra/terraform/azure/prod/README.md` for environment-specific notes.

2.cert-manager will automatically provision and renew TLS certificates for your domains (e.g., api.copilotgtm.com, devapi.copilotgtm.com).

### Notes

- This two-step process is required because cert-manager CRDs must be present before you can create ClusterIssuer or Certificate resources.
- You only need to repeat Step 2 if you change certificate details or add new domains.
- For more details, see `cert-manager-step1.tf` and `cert-manager-step2.tf` in the `infra/terraform/azure/dev` directory.

terraform apply -target="helm_release.cert_manager"
terraform apply -auto-approve
