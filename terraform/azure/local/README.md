# Local Development Infrastructure

This directory contains the Terraform configuration for deploying a simplified, public-access infrastructure for local development and testing purposes.

## Target Environment

- **Region**: Central India
- **Resource Group**: From `resource_group_name` in `terraform.tfvars`

## Resources Created

### 1. Azure Resource Group

- **Name**: From `resource_group_name` in `terraform.tfvars`
- **Location**: Central India

### 2. Azure Key Vaults (2)

#### 2.1 Database Key Vault

- **Name**: `kv-local-${project_name}`
- **Access**: Public network access enabled.
- **Used for**: Storing the database admin password secret.

#### 2.2 Master Key Vault

- **Name**: `kvm-${env}-${project_name}`
- **Access**: Public network access enabled.
- **Used for**: Mirrors the `dev` “master” key vault pattern.

### 3. Azure Storage Accounts (2)

#### 3.1 Master Storage Account

- **Name**: From `storage_account_name` in `terraform.tfvars`
- **Access**: Public network access enabled.
- **Container**: `master` (Private access)

#### 3.2 Organisation Storage Account

- **Name**: From `org_storage_account_name` in `terraform.tfvars`
- **Access**: Public network access enabled.
- **Container**: `organisation` (Private access)

### 4. PostgreSQL Flexible Server

- **Name**: `${db_name}-${env}` (the PostgreSQL module appends the env suffix)
- **Version**: 14
- **Location**: Central India
- **Access**: Public network access enabled (0.0.0.0/0 allowed).
- **Admin User**: `CopilotGTMAdmin`
- **Admin Password**: Randomly generated 32-character string.

### 5. Azure OpenAI

- **Account**: `openai-${project_name}-${env}`
- **Region**: `eastus2`
- **Deployments**:
   - `gpt-5` (model `gpt-5`, version `2025-08-07`, SKU `GlobalStandard`)
   - `gpt-5-mini` (model `gpt-5-mini`, version `2025-08-07`, SKU `GlobalStandard`)
   - `gpt-5.2` (model `gpt-5.2`, version `2025-12-11`, SKU `GlobalStandard`)
   - `text-embedding-3-large` (model `text-embedding-3-large`, version `1`, SKU `Standard`)

## Token-per-minute (TPM) limits

Azure OpenAI **tokens per minute (TPM)** and **requests per minute (RPM)** are **quota/rate limits** that are enforced **per region, per subscription, per model/deployment type**.

You **can allocate TPM to a specific deployment** using the deployment SKU capacity:

- In Terraform this is `azurerm_cognitive_deployment.sku.capacity`.
- Microsoft documents that **`capacity = 1` equals `1,000 TPM`** (and capacity is adjusted in increments of 1,000 TPM).

This allocation is still bounded by your **approved quota** for that model/deployment type in that region. You also don't set TPM and RPM independently; Azure enforces an RPM value proportional to the TPM allocation (ratio varies by model).

What you *can* do:

- **If you need at least 500k TPM throughput**: verify your subscription quota in the Foundry portal and request an increase if needed.
   - Quota page: https://ai.azure.com/resource/quota
   - Quota increase request: https://aka.ms/oai/stuquotarequest
- **If you want to cap usage at 500k TPM for cost control**: implement throttling in your application (token bucket / rate limiter) or put a proxy in front (for example API Management) and enforce limits there.

### Quick check: subscription offer/quota ID

Some Azure offer types have lower default quotas. You can check the offer/quota ID:

```powershell
$subId = "<subscription-guid>"
$token = az account get-access-token --resource https://management.azure.com/ --query accessToken -o tsv
$uri = "https://management.azure.com/subscriptions/${subId}?api-version=2020-01-01"
Invoke-RestMethod -Method Get -Uri $uri -Headers @{ Authorization = "Bearer $token" } |
   Select-Object -ExpandProperty subscriptionPolicies |
   Select-Object locationPlacementId, quotaId, spendingLimit
```

## Usage

1. **Initialize Terraform**:

   ```bash
   terraform init
   ```

2. **Deploy Resources**:

   ```bash
   terraform apply
   ```

3. **Outputs**:
   After a successful apply, the following information will be printed:
   - `resource_group_name`
   - `key_vault_uri`
   - `master_key_vault_uri`
   - `storage_account_name`
   - `org_storage_account_name`
   - `database_host`
   - `database_admin_user`
   - `database_password` (Visible in plain text as requested)
