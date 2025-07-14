# Azure Modular Terraform Infrastructure

This project uses a modular Terraform architecture with all configuration stored within the `terraform/` directory. Modules are stored in `terraform/modules/` to keep the root configuration and reusable components cleanly separated. The first module provisions an Azure Resource Group, laying the foundation for additional cloud components (e.g., Kafka, Redis, WebSockets, Kubernetes) in a scalable and maintainable manner.

## Project Structure

```
terraform/
  main.tf
  variables.tf
  provider.tf
  outputs.tf
  terraform.tfvars
  modules/
    resource_group/
      main.tf
      variables.tf
      outputs.tf
```

## Prerequisites

- [Terraform](https://www.terraform.io/downloads.html) (>= 1.3.0)
- [Azure CLI](https://docs.microsoft.com/en-us/cli/azure/install-azure-cli) (for authentication)
- An active Azure subscription

## Setup & Usage

### 1. Clone the repository and navigate to the Terraform directory

```sh
cd terraform
```

### 2. Authenticate with Azure

Login using Azure CLI if not already authenticated:

```sh
az login
```

> Optional: Set the correct Azure subscription if you have more than one.
>
> ```sh
> az account set --subscription "Your Subscription Name or Id"
> ```

### 3. Initialize Terraform

```sh
terraform init
```

### 4. Configure Input Variables

You can provide variables via a `.tfvars` file or directly with `-var` flags.

#### Example: `terraform.tfvars` file

Edit the existing `terraform.tfvars` file:

```hcl
env                 = "dev"
subscription_id     = "your-azure-subscription-id"
resource_group_name = "my-terraform-rg" # The actual resource group created will be suffixed with -dev
location            = "East US"
vnet_address_space  = ["10.0.0.0/16"]
```

### 5. Plan the Deployment

To guarantee consistent deployment, save the execution plan to a file using the `-out` option:

```sh
terraform plan -var-file="terraform.tfvars" -out=tfplan
```

### 6. Apply the Deployment

To apply exactly what was planned, use the saved plan file:

```sh
terraform apply tfplan
```

### 7. Review Outputs

After apply, Terraform will output the details of the created Azure Resource Group.

## Cleaning Up / Destroying Resources

To destroy all resources created by this Terraform stack, use:

```sh
terraform destroy -var-file="terraform.tfvars"
```

Terraform will show a plan of what will be destroyed and prompt you for confirmation. **This will permanently delete your Azure resources managed by this configuration.**

## Extending the Infrastructure

Modules are located under `terraform/modules/`, e.g., `resource_group`. To add new components like Kafka, Redis, or WebSocket infrastructure, create new subfolders in `modules/` and reference them in `main.tf`:

```hcl
module "kafka" {
  source = "./modules/kafka"
  # module-specific variables
}
```

This structure makes it easy to grow your cloud infrastructure in a clean, reusable, and scalable way.

---
## Setting Up a Production-Grade Terraform State Backend in Azure

**Follow these steps before running any Terraform plan/apply:**

### 1. Create a dedicated resource group for Terraform state
```sh
az group create --name gtm-tfstate --location eastus
```

### 2. Create a globally unique storage account (choose your own unique name)
```sh
az storage account create --name gtmstatestor001 --resource-group gtm-tfstate --location eastus --sku Standard_LRS --encryption-services blob
```

### 3. Enable blob versioning and soft delete for data protection
```sh
az storage account blob-service-properties update --account-name gtmstatestor001 --enable-change-feed true
az storage account blob-service-properties update --account-name gtmstatestor001 --enable-delete-retention true --delete-retention-days 30
```

### 4. Create a blob container for Terraform state files
```sh
az storage container create --name tfstate --account-name gtmstatestor001
```

### 5. Example backend block for your Terraform config:
```hcl
terraform {
  backend "azurerm" {
    resource_group_name  = "gtm-tfstate"
    storage_account_name = "gtmstatestor001"
    container_name       = "tfstate"
    key                  = "azure/dev/terraform.tfstate"
  }
}
```
**Tip:** Use a different `key` for each environment (dev, uat, prod) to isolate state.

---

**Ensure you have the correct permissions to create these resources in Azure. Run all commands from your terminal or CI/CD pipeline before Terraform deployment.**
> **UPDATE:**
> If you encounter "ParentResourceNotFound" errors during blob property updates, explicitly specify the resource group using `--resource-group`:
```sh
az storage account blob-service-properties update --account-name gtmstatestor001 --resource-group gtm-tfstate --enable-change-feed true
az storage account blob-service-properties update --account-name gtmstatestor001 --resource-group gtm-tfstate --enable-delete-retention true --delete-retention-days 30
```

---

## Automated cert-manager and TLS Certificate Setup (Production-Ready)

This project uses a two-step, production-grade approach to automate cert-manager and TLS certificate provisioning for Kubernetes Ingress.

### Prerequisites

- Terraform installed
- kubectl access to your AKS cluster
- Azure credentials configured

### Step 1: Install cert-manager and CRDs

1. Navigate to the `terraform/azure/dev` directory:
   ```sh
   cd terraform/azure/dev
   ```

2. Apply only the cert-manager Helm release (this will install cert-manager and its CRDs):
   ```sh
   terraform apply -target=helm_release.cert_manager
   ```
   > If you see "Invalid target" errors, ensure the resource is named exactly `helm_release.cert_manager` in your .tf files and that the file (e.g., `cert-manager-step1.tf`) is present in the current directory. You can also run a normal `terraform apply` to apply all resources in the step if needed.

3. Wait until cert-manager is running and the CRDs are present. You can check with:
   ```sh
   kubectl get crd | grep cert-manager
   ```
   Make sure you see resources like `clusterissuers.cert-manager.io`.

### Step 2: Create ClusterIssuer and Certificate

1. Apply the ClusterIssuer and Certificate resources:
   ```sh
   terraform apply -target=kubernetes_manifest.letsencrypt_prod_issuer -target=kubernetes_manifest.api_cert
   ```

2. cert-manager will automatically provision and renew TLS certificates for your domains (e.g., api.copilotgtm.com, devapi.copilotgtm.com).

### Notes

- This two-step process is required because cert-manager CRDs must be present before you can create ClusterIssuer or Certificate resources.
- You only need to repeat Step 2 if you change certificate details or add new domains.
- For more details, see `cert-manager-step1.tf` and `cert-manager-step2.tf` in the `terraform/azure/dev` directory.


terraform apply -target="helm_release.cert_manager"
terraform apply -auto-approve
