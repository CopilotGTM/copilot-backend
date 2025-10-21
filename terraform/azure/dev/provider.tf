terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 4.0.0"  # Required for node pool autoscaling
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
}

# AzureAD provider required for workload identity (AAD app + federated credential)
provider "azuread" {}

# Azure DevOps provider for pipeline/resource automation
provider "azuredevops" {
  org_service_url       = var.org_service_url   # Set via tfvars or command line
  personal_access_token = var.azuredevops_pat   # Pass securely via environment variable or tfvars (never hardcode)
}


provider "helm" {
  kubernetes = {
    host                   = module.aks.host
    client_certificate     = base64decode(module.aks.client_certificate)
    client_key             = base64decode(module.aks.client_key)
    cluster_ca_certificate = base64decode(module.aks.cluster_ca_certificate)
  }
}

provider "kubernetes" {
  host                   = module.aks.host
  client_certificate     = base64decode(module.aks.client_certificate)
  client_key             = base64decode(module.aks.client_key)
  cluster_ca_certificate = base64decode(module.aks.cluster_ca_certificate)
}
