terraform {
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = ">= 2.11.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = ">= 2.5.1"
    }
    azurerm = {
      source = "hashicorp/azurerm"
      version = ">= 2.0"
    }
  }
  required_version = ">= 1.3.0"

  backend "azurerm" {
    resource_group_name  = "gtm-tfstate"
    storage_account_name = "gtmstatestor001"
    container_name       = "tfstate"
    key                  = "k8s.terraform.tfstate"
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
  use_cli         = true
}

data "terraform_remote_state" "aks" {
  backend = "azurerm"

  config = {
    resource_group_name  = "gtm-tfstate"
    storage_account_name = "gtmstatestor001"
    container_name       = "tfstate"
    key                  = "azure/dev/terraform.tfstate"
  }
}

provider "kubernetes" {
  host                   = data.terraform_remote_state.aks.outputs.aks_host
  client_certificate     = base64decode(data.terraform_remote_state.aks.outputs.aks_client_certificate)
  client_key             = base64decode(data.terraform_remote_state.aks.outputs.aks_client_key)
  cluster_ca_certificate = base64decode(data.terraform_remote_state.aks.outputs.aks_cluster_ca_certificate)
}

provider "helm" {
  kubernetes = {
    host                   = data.terraform_remote_state.aks.outputs.aks_host
    client_certificate     = base64decode(data.terraform_remote_state.aks.outputs.aks_client_certificate)
    client_key             = base64decode(data.terraform_remote_state.aks.outputs.aks_client_key)
    cluster_ca_certificate = base64decode(data.terraform_remote_state.aks.outputs.aks_cluster_ca_certificate)
  }
}
