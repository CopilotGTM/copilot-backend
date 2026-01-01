terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 4.0.0" # Required for node pool autoscaling
    }
    azuread = {
      source  = "hashicorp/azuread"
      version = ">= 2.48.0"
    }
    azuredevops = {
      source  = "microsoft/azuredevops"
      version = ">= 0.2.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = ">= 2.5.1"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.11.0"
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.1.0"
    }
    azapi = {
      source  = "azure/azapi"
      version = ">= 2.5.0"
    }
    kubectl = {
      source  = "gavinbunney/kubectl"
      version = ">= 1.14.0"
    }
  }
  required_version = ">= 1.3.0"

  backend "azurerm" {
    resource_group_name  = "gtm-tfstate"
    storage_account_name = "gtmstatestor001"
    container_name       = "tfstate"
    key                  = "azure/dev/terraform.tfstate"
  }

}

provider "azurerm" {
  features {
    resource_group {
      prevent_deletion_if_contains_resources = false
    }
  }
  subscription_id = var.subscription_id
  # OIDC auth is configured via ARM_* environment variables:
  # ARM_USE_OIDC, ARM_OIDC_TOKEN, ARM_CLIENT_ID, ARM_TENANT_ID, ARM_SUBSCRIPTION_ID
}

data "azurerm_key_vault_secret" "azuredevops_pat" {
  count        = var.azuredevops_pat == "" && var.azuredevops_pat_secret_name != "" ? 1 : 0
  name         = var.azuredevops_pat_secret_name
  key_vault_id = "/subscriptions/${var.subscription_id}/resourceGroups/${var.resource_group_name}-${var.env}/providers/Microsoft.KeyVault/vaults/copilot-gtm-kv-${var.env}"
}

locals {
  effective_azuredevops_pat = var.azuredevops_pat != "" ? var.azuredevops_pat : try(data.azurerm_key_vault_secret.azuredevops_pat[0].value, null)
}

# AzureAD provider required for workload identity (AAD app + federated credential)
# OIDC auth is configured via ARM_* environment variables (same as azurerm)
provider "azuread" {}

# Azure DevOps provider for pipeline/resource automation
provider "azuredevops" {
  org_service_url       = var.org_service_url # Set via tfvars or command line
  personal_access_token = local.effective_azuredevops_pat
}



provider "helm" {
  kubernetes = {
    host                   = try(module.aks.host, null)
    client_certificate     = try(base64decode(module.aks.client_certificate), null)
    client_key             = try(base64decode(module.aks.client_key), null)
    cluster_ca_certificate = try(base64decode(module.aks.cluster_ca_certificate), null)
  }
}

provider "kubernetes" {
  host                   = try(module.aks.host, null)
  client_certificate     = try(base64decode(module.aks.client_certificate), null)
  client_key             = try(base64decode(module.aks.client_key), null)
  cluster_ca_certificate = try(base64decode(module.aks.cluster_ca_certificate), null)
}

# kubectl provider for resources that depend on CRDs (doesn't validate during plan)
provider "kubectl" {
  host                   = try(module.aks.host, null)
  client_certificate     = try(base64decode(module.aks.client_certificate), null)
  client_key             = try(base64decode(module.aks.client_key), null)
  cluster_ca_certificate = try(base64decode(module.aks.cluster_ca_certificate), null)
  load_config_file       = false
}

