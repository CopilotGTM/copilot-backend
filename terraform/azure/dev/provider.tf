terraform {
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = ">= 3.0"
    }
    azuredevops = {
      source  = "microsoft/azuredevops"
      version = ">= 0.2.0"
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

# Azure DevOps provider for pipeline/resource automation
provider "azuredevops" {
  org_service_url       = var.org_service_url   # Set via tfvars or command line
  personal_access_token = var.azuredevops_pat   # Pass securely via environment variable or tfvars (never hardcode)
}