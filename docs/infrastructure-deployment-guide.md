# Azure Infrastructure Deployment (Terraform)

This repository deploys Azure infrastructure via Terraform in **two steps**:

1. **Core Azure foundation** (`terraform/azure/<env>/`)
2. **AKS add-ons / in-cluster platform** (`terraform/azure/<env>/k8s/`)

The guidance below is **environment-agnostic**. Use it for any environment folder (`dev`, `uat`, `prod`) by swapping `<env>` and supplying the appropriate inputs.

## Repo structure

```
terraform/
    azure/
        <env>/
            main.tf
            provider.tf
            variables.tf
            outputs.tf
            terraform.tfvars        # keep local / secure (do not commit)
            k8s/
                main.tf
                provider.tf
                varibales.tf
                terraform.tfvars      # keep local / secure (do not commit)
        modules/
            ...
docs/
    infrastructure-deployment-guide.md
    keda-autoscaling-guide.md
charts/
    ...
```

Why two steps:

- Step 1 must create AKS, ACR, networking, identity, data services, and DevOps automation.
- Step 2 depends on AKS being up (it uses remote state outputs to connect via the Kubernetes/Helm providers).

## What this deploys (service inventory)

### Step 1: Core Azure foundation (`terraform/azure/<env>/`)

Provisioned services and capabilities:

- **Resource group** (environment-suffixed)
- **Networking** via VNet module
  - VNet, public + private subnets
  - NAT/public IP outputs (as exposed by the module)
- **Azure Cache for Redis**
- **Key Vaults**
  - database Key Vault (DB secrets)
  - master Key Vault (shared/master secrets)
- **Storage accounts**
  - master storage (general)
  - organisation storage (per-organisation data)
  - blob CORS configured for frontend origins
- **Azure Container Registry (ACR)**
- **Azure Kubernetes Service (AKS)**
  - optional GPU/compute node pool settings (controlled by variables)
  - OIDC issuer enabled (required for workload identity)
- **Workload identity (Entra ID)**
  - backend workload identity principal
  - workers workload identity principal
- **RBAC role assignments**
  - AKS kubelet gets `AcrPull` on ACR
  - workload identities get scoped access to Key Vault + Storage
- **PostgreSQL Flexible Server** (module-managed)
  - admin password generated via `random_password`
  - connection details exported for app usage
- **Azure AI Foundry** (module-managed)
  - OpenAI endpoint details exported for app usage
- **App Services** (Linux, container-based)
  - frontend web app
  - admin frontend web app
  - optional custom domain + DNS record creation (module-controlled)
- **Azure DevOps automation**
  - service principal for pipelines/service connections
  - Azure DevOps service connections (Azure RM + ACR)
  - variable group for runtime config + secrets
  - pipelines (backend, frontend, admin)
  - secure file for kubeconfig (generated from AKS outputs)

### Step 2: AKS add-ons (`terraform/azure/<env>/k8s/`)

Provisioned in-cluster components and DNS wiring:

- **NGINX Ingress Controller** (`ingress-nginx`) with a public LoadBalancer service
- **cert-manager** (installs CRDs)
- **Let’s Encrypt ClusterIssuer**
- **TLS Certificate** for the API hostname (pattern typically: `<env>api.<zone>`)
- **Application namespace**
- **KEDA** (Kubernetes Event-driven Autoscaling) when enabled
- **DNS A record** pointing at the ingress LoadBalancer IP

For KEDA operational guidance, also see `docs/keda-autoscaling-guide.md`.

## Prerequisites

- Terraform `>= 1.3`
- Azure CLI
- (Recommended) `kubectl` for verification

You also need permissions to:

- create/manage Azure resources in the subscription
- edit DNS records for the application DNS zone
- create/manage Azure DevOps pipelines, variable groups, secure files, and service connections

## Terraform state backend (required)

Both stacks use the AzureRM backend. Create the backend resources once:

```sh
az group create --name gtm-tfstate --location eastus
az storage account create --name <globally-unique-name> --resource-group gtm-tfstate --location eastus --sku Standard_LRS
az storage container create --name tfstate --account-name <globally-unique-name>
```

State key conventions:

- Core: `azure/<env>/terraform.tfstate`
- K8s: **recommended** `azure/<env>/k8s/terraform.tfstate`

If the k8s stack uses a shared key (example: `k8s.terraform.tfstate`), different environments can overwrite each other’s state.

## Inputs and secrets (important)

Do not commit real secrets into `terraform.tfvars`.

Preferred patterns:

- pass sensitive values from CI using environment variables (`TF_VAR_*`)
- store secrets in Azure DevOps variable groups / secret variables
- check in a `terraform.tfvars.example` file containing placeholders only

Note: `.gitignore` already ignores `*.tfvars`, but git will still track a file if it was committed before the ignore rule existed.

If a real `terraform.tfvars` (or any `.tfstate`) was accidentally committed, remove it from the index and rotate the secrets:

```sh
git rm --cached terraform/azure/<env>/terraform.tfvars
git rm --cached terraform/azure/<env>/k8s/terraform.tfvars
git commit -m "Stop tracking tfvars"
```

## Deploy: two-step process

### Step 1 — Core Azure foundation

```sh
cd terraform/azure/<env>
terraform init
terraform plan -var-file="terraform.tfvars" -out=tfplan
terraform apply tfplan
```

Verification you should do after Step 1:

- confirm AKS created and outputs are present
- confirm ACR created
- confirm DevOps service connections and variable group exist

### Step 2 — AKS add-ons

```sh
cd terraform/azure/<env>/k8s
terraform init
terraform plan -var-file="terraform.tfvars" -out=tfplan
terraform apply tfplan
```

Recommended verification:

```sh
kubectl get ns
kubectl get pods -n ingress-nginx
kubectl get pods -n cert-manager
kubectl get pods -n keda-system
kubectl get clusterissuer
kubectl get certificate -A
```

## Destroy / teardown

Destroy in reverse order:

```sh
cd terraform/azure/<env>/k8s
terraform destroy -var-file="terraform.tfvars"

cd ../
terraform destroy -var-file="terraform.tfvars"
```
