# Production-Grade Azure Infrastructure with Terraform

This documentation provides a comprehensive guide to deploying, managing, and extending the Azure infrastructure using the modular Terraform setup in this repository.

## 1. Introduction

This project automates the provisioning of a scalable and secure Azure cloud environment using a modular Terraform architecture. It is designed to support production-grade workloads with a focus on best practices for state management, security, and CI/CD automation.

### Key Features:
- **Modular Design**: Reusable Terraform modules for components like AKS, ACR, PostgreSQL, and more.
- **Environment Isolation**: Separate state files and configurations for `dev`, `uat`, and `prod` environments.
- **Secure State Management**: Centralized and protected Terraform state using Azure Blob Storage.
- **Automated TLS**: Production-ready TLS certificate management with `cert-manager` and Let's Encrypt.
- **CI/CD Ready**: Pipelines for automated infrastructure deployment and management.

## 2. Project Structure

The repository is organized to promote separation of concerns and reusability.

```
terraform/
├── azure/
│   ├── dev/
│   │   ├── main.tf         # Dev environment root configuration
│   │   ├── variables.tf    # Dev environment variables
│   │   └── terraform.tfvars # Dev environment variable values
│   ├── uat/
│   └── prod/
└── modules/
    ├── aks/                # AKS cluster module
    ├── acr/                # Azure Container Registry module
    ├── resource_group/     # Resource group module
    └── ...                 # Other reusable modules
```

- **`terraform/azure/{env}`**: Contains the root configuration for each environment.
- **`terraform/modules`**: Contains reusable modules that can be instantiated in any environment.

## 3. Prerequisites

Before you begin, ensure you have the following tools installed and configured:

- **Terraform** (`>= 1.3.0`)
- **Azure CLI** (for authentication)
- **kubectl** (for interacting with Kubernetes)
- An active **Azure Subscription** with the necessary permissions.

## 4. Getting Started: Step-by-Step Deployment

### Step 4.1: Configure Azure Authentication

First, log in to your Azure account. This will store your credentials locally for Terraform to use.

```sh
az login
az account set --subscription "Your-Subscription-ID-or-Name"
```

### Step 4.2: Set Up Production-Grade Terraform Backend

For a production environment, it is critical to store the Terraform state file in a secure, remote location. This prevents state loss and allows for collaboration.

1.  **Create a dedicated resource group for the backend:**
    ```sh
    az group create --name gtm-tfstate --location eastus
    ```

2.  **Create a storage account (must be globally unique):**
    ```sh
    az storage account create --name gtmstatestor001 --resource-group gtm-tfstate --sku Standard_LRS --encryption-services blob
    ```

3.  **Enable versioning and soft delete for data protection:**
    ```sh
    az storage account blob-service-properties update --account-name gtmstatestor001 --resource-group gtm-tfstate --enable-change-feed true --enable-delete-retention true --delete-retention-days 30
    ```

4.  **Create a blob container:**
    ```sh
    az storage container create --name tfstate --account-name gtmstatestor001
    ```

### Step 4.3: Initialize and Deploy an Environment

Navigate to the desired environment's directory (e.g., `dev`) and initialize Terraform. This will configure the backend you just created.

1.  **Navigate to the environment directory:**
    ```sh
    cd terraform/azure/dev
    ```

2.  **Update the `provider.tf` with your backend details:**

    ```hcl
    terraform {
      backend "azurerm" {
        resource_group_name  = "gtm-tfstate"
        storage_account_name = "gtmstatestor001"
        container_name       = "tfstate"
        key                  = "azure/dev/terraform.tfstate" # Use a unique key per environment
      }
    }
    ```

3.  **Initialize Terraform:**
    ```sh
    terraform init
    ```

4.  **Plan and Apply the infrastructure:**
    ```sh
    terraform plan -out=tfplan
    terraform apply tfplan
    ```

## 5. Automated TLS with Cert-Manager

This project includes an automated, two-step process for provisioning TLS certificates in your AKS cluster.

### Step 5.1: Install Cert-Manager and CRDs

The first step is to install `cert-manager`, which manages the lifecycle of your certificates.

```sh
# From terraform/azure/dev
terraform apply -target=helm_release.cert_manager
```

Wait for the installation to complete and verify that the Custom Resource Definitions (CRDs) are available:

```sh
kubectl get crd | grep cert-manager
```

### Step 5.2: Create ClusterIssuer and Certificate

Once the CRDs are ready, you can create a `ClusterIssuer` (which represents a certificate authority) and a `Certificate` resource.

```sh
# From terraform/azure/dev
terraform apply -target=kubernetes_manifest.letsencrypt_prod_issuer -target=kubernetes_manifest.api_cert
```

`cert-manager` will now automatically issue and renew TLS certificates for the domains specified in your configuration.

## 6. CI/CD Pipelines

This project is designed to be integrated with a CI/CD pipeline for automated infrastructure deployments. The pipelines should perform the following steps:

1.  **Lint and Validate**: Check Terraform code for syntax errors and formatting issues.
2.  **Terraform Plan**: Generate an execution plan and store it as an artifact.
3.  **Manual Approval**: Require a manual approval step before applying changes to production.
4.  **Terraform Apply**: Apply the saved plan to the target environment.

## 7. Extending the Infrastructure

To add new infrastructure components (e.g., a Redis cache or a Kafka cluster), follow these steps:

1.  **Create a new module** in the `terraform/modules/` directory.
2.  **Define input variables and outputs** for the module.
3.  **Instantiate the module** in the root `main.tf` of the desired environment(s).
4.  **Run `terraform plan` and `terraform apply`** to deploy the new resources.

## 8. Destroying Resources

To tear down all resources managed by a Terraform configuration, run the `destroy` command. **This is irreversible and will permanently delete your infrastructure.**

```sh
# From the environment directory (e.g., terraform/azure/dev)
terraform destroy