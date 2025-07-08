# -----------------------------------------------------------------
# Resource Group: Top-level container for all Azure resources
# -----------------------------------------------------------------
module "resource_group" {
  source              = "../modules/resource_group"
  resource_group_name = "${var.resource_group_name}-${var.env}"
  location            = var.location
}

# -----------------------------------------------------------------
# Virtual Network: Provides networking and subnets for AKS and App Services
# -----------------------------------------------------------------
module "vnet" {
  source              = "../modules/vnet"
  vnet_name           = "${var.vnet_name}-${var.env}"
  address_space       = var.vnet_address_space
  location            = module.resource_group.resource_group_location
  resource_group_name = module.resource_group.resource_group_name
  public_subnets      = var.vnet_public_subnets
  private_subnets     = var.vnet_private_subnets
  env                 = var.env
  depends_on          = [
    module.resource_group
  ]
}

# -----------------------------------------------------------------
# Azure Key Vault
# -----------------------------------------------------------------
module "key_vault" {
  source              = "../modules/key_vault"
  key_vault_name      = "copilotgtm-vault-${var.env}"
  location            = var.location
  resource_group_name = module.resource_group.resource_group_name
  tenant_id           = data.azurerm_client_config.current.tenant_id
  admin_object_id     = data.azurerm_client_config.current.object_id
  tags = {
    environment = var.env
    project     = var.project_name
  }
  depends_on = [
    module.resource_group
  ]
}

# # -----------------------------------------------------------------
# # Azure Storage Account: General purpose blob/file storage
# # -----------------------------------------------------------------
# module "storage_account" {
#   source                  = "../modules/storage_account"
#   storage_account_name    = "${var.storage_account_name}${var.env}"
#   resource_group_name     = module.resource_group.resource_group_name
#   location                = var.location
#   account_tier            = "Standard"
#   account_replication_type = "LRS"
#   tags = {
#     environment = var.env
#     project     = var.project_name
#   }
# }

# -----------------------------------------------------------------
# Azure Container Registry: Stores container images for AKS and DevOps pipelines
# -----------------------------------------------------------------
module "acr" {
  source              = "../modules/acr"
  acr_name            = "${var.acr_name}${var.env}"
  resource_group_name = module.resource_group.resource_group_name
  location            = var.location
  sku                 = var.acr_type
  subnet_id           = module.vnet.private_subnet_ids[0]
  vnet_id             = module.vnet.vnet_id
  depends_on          = [
    module.resource_group,
    module.vnet
  ]
}

# -----------------------------------------------------------------
# Azure Kubernetes Service: Managed Kubernetes cluster for containers
# -----------------------------------------------------------------
module "aks" {
  source               = "../modules/aks"
  aks_name             = "${var.aks_name}-${var.env}"
  dns_prefix           = "${var.aks_dns_prefix}-${var.env}"
  location             = module.resource_group.resource_group_location
  resource_group_name  = module.resource_group.resource_group_name
  subnet_id            = module.vnet.private_subnet_ids[0]
  agent_count          = var.aks_node_count
  vm_size              = var.aks_vm_size
  os_disk_size_gb      = var.aks_os_disk_size_gb
  service_cidr         = var.aks_service_cidr
  dns_service_ip       = var.aks_dns_service_ip
  env                  = var.env
  depends_on           = [
    module.vnet,
    module.acr
  ]
}

# -----------------------------------------------------------------
# Cert-Manager: Kubernetes certificate management via Helm (Stage 1)
# -----------------------------------------------------------------
module "cert_manager" {
  source                        = "../modules/cert_manager"
  aks_dependency                = module.aks
  ingress_controller_dependency = module.ingress_controller
}

# -----------------------------------------------------------------
# Cert-Manager Issuers and Certificates (Stage 2)
# To be applied after cert-manager is installed and ready.
# 
# Two-stage apply process:
#   1. terraform apply -target=module.aks -target=module.ingress_controller -target=azurerm_dns_a_record.devapi -target=module.cert_manager
#   2. terraform apply
# This ensures cert-manager CRDs/controllers are ready before creating ClusterIssuer/Certificate resources.
# -----------------------------------------------------------------
module "cert_manager_issuers" {
  source                         = "../modules/cert_manager_issuers"
  cert_manager_install_dependency = module.cert_manager
  letsencrypt_email              = "nikhil@copilotgtm.com"
  dns_record_dependency          = azurerm_dns_a_record.devapi
  devapi_dns_name                = "devapi.copilotgtm.com"
}

# -----------------------------------------------------------------
# Azure Redis Cache: In-memory data store for caching and sessions
# -----------------------------------------------------------------
module "redis" {
  source              = "../modules/redis"
  redis_name          = "${var.redis_name}-${var.env}"
  resource_group_name = module.resource_group.resource_group_name
  location            = var.location
  capacity            = var.redis_capacity
  family              = var.redis_family
  sku_name            = var.redis_sku_name
  tags = {
    environment = var.env
    project     = var.project_name
  }
  depends_on = [
    module.resource_group,
    module.vnet
  ]
}

# App Service: Azure Web App for frontend hosting (Node.js)
# -----------------------------------------------------------------
module "frontend_app_service" {
  source                  = "../modules/app_service"
  app_service_plan_name   = "${var.frontend_app_service_plan_name}-${var.env}"
  app_service_name        = "${var.frontend_app_service_name}-${var.env}"
  resource_group_name     = module.resource_group.resource_group_name
  location                = var.location
  node_version            = var.frontend_node_version
  app_settings            = {}
  sku_name                = var.frontend_sku_name
  os_type                 = var.frontend_os_type
  https_only              = var.frontend_https_only
  depends_on = [
    module.resource_group
  ]
}

# -----------------------------------------------------------------
# Role Assignment: Allow AKS kubelet to pull images from ACR
# -----------------------------------------------------------------
resource "azurerm_role_assignment" "aks_kubelet_acr_pull" {
  principal_id         = module.aks.kubelet_identity_object_id
  role_definition_name = "AcrPull"
  scope                = module.acr.acr_id
  skip_service_principal_aad_check = true
  depends_on           = [
    module.acr,
    module.aks
  ]
}

# -----------------------------------------------------------------
# Role Assignment: Allow App Service to pull images from ACR
# -----------------------------------------------------------------
resource "azurerm_role_assignment" "app_service_acr_pull" {
  principal_id         = module.frontend_app_service.app_service_principal_id
  role_definition_name = "AcrPull"
  scope                = module.acr.acr_id
  principal_type       = "ServicePrincipal"
  depends_on           = [
    module.acr,
    module.frontend_app_service
  ]
}

# -----------------------------------------------------------------
# Service Principal for Azure DevOps
# -----------------------------------------------------------------
module "service_principal" {
  source          = "../modules/service_principal"
  sp_name         = var.sp_name
  subscription_id = var.subscription_id
}

# -----------------------------------------------------------------
# Azure DevOps Service Connection (for ACR/AKS)
# -----------------------------------------------------------------
data "azuredevops_project" "project" {
  name = var.devops_project_name
}

module "service_connection" {
  source                   = "../modules/service_connection"
  devops_project_id        = data.azuredevops_project.project.id
  service_connection_name  = "copilotgtm-arc-ci-cd-service--${var.env}"
  subscription_id          = var.subscription_id
  subscription_name        = "Copilot GTM Subscription"
  tenant_id                = module.service_principal.tenant_id
  client_id                = module.service_principal.client_id
  client_secret            = module.service_principal.client_secret
}

# -----------------------------------------------------------------
# Azure DevOps Pipeline (CI/CD)
# -----------------------------------------------------------------
data "azuredevops_git_repository" "repo" {
  project_id = data.azuredevops_project.project.id
  name       = var.backend_repo_name
}

module "pipeline" {
  source            = "../modules/pipeline"
  devops_project_id = data.azuredevops_project.project.id
  pipeline_name     = var.backend_pipeline_name
  repo_id           = data.azuredevops_git_repository.repo.id
  branch_name       = "main"
  yml_path          = var.backend_yml_path
}

# -----------------------------------------------------------------
# Azure DevOps Service Connection for ACR (Docker Registry)
# -----------------------------------------------------------------
module "service_connection_acr" {
  source                = "../modules/service_connection_acr"
  devops_project_id     = data.azuredevops_project.project.id
  service_endpoint_name = "copilotgtm-acr-connection-${var.env}"
  acr_login_server      = module.acr.acr_login_server
  acr_sp_app_id         = module.service_principal.client_id
  acr_sp_app_secret     = module.service_principal.client_secret
}

# -----------------------------------------------------------------
# Ingress Controller (nginx) for AKS
# -----------------------------------------------------------------
module "ingress_controller" {
  source         = "../modules/ingress-controller"
  release_name   = "nginx-ingress"
  namespace      = "ingress-nginx"
  chart_version  = "4.10.1"
  service_type   = "LoadBalancer"
  replica_count  = 1
  aks_dependency = module.aks
}

data "azurerm_client_config" "current" {}

# -----------------------------------------------------------------
# DNS A Record for devapi.copilotgtm.com -> nginx ingress public IP
# -----------------------------------------------------------------
resource "azurerm_dns_a_record" "devapi" {
  name                = "devapi"
  zone_name           = "copilotgtm.com"
  resource_group_name = "master-resources"
  ttl                 = 300
  records             = [module.ingress_controller.nginx_ingress_public_ip]
  depends_on          = [module.ingress_controller]
}

# -----------------------------------------------------------------
# Generate a strong, random DB admin password
# -----------------------------------------------------------------
resource "random_password" "db_admin" {
  length           = 32                # total characters
  override_special = "!@#$%&*()-_=+[]" # allowed special chars
  special          = true
  upper            = true
  lower            = true
  numeric          = true
}

# # -----------------------------------------------------------------
# # PostgreSQL Flexible Server
# # -----------------------------------------------------------------
# module "postgresql" {
#   source                   = "../modules/postgresql"
#   env                      = var.env
#   resource_group_name      = module.resource_group.resource_group_name
#   location                 = "westus"
#   db_admin_user            = "CopilotGTMAdmin"
#   db_admin_password        = random_password.db_admin.result
#   key_vault_id             = module.key_vault.key_vault_id
#   tags                     = {
#     environment = var.env
#     project     = var.project_name
#   }
#   depends_on = [
#     module.resource_group,
#     module.key_vault
#   ]
# }


resource "azuredevops_variable_group" "secrets" {
  project_id = data.azuredevops_project.project.id
  name       = "app-secrets-${var.env}"
  description = "Service-principal creds and other secrets for ${var.env}"

  variable {
    name         = "ENV"
    value        = "dev"
  }

  variable {
    name         = "CLOUD_PROVIDER"
    value        = "azure"
  }

  variable {
    name         = "APPLICATION_REGION"
    value        = "eastus2"
  }

  variable {
    name         = "APPLICATION_STORAGE_NAME"
    value        = "dev-app"
  }

  variable {
    name         = "DATABASE_HOST"
    value        = "dev-copilot-gtm.postgres.database.azure.com"
    # value        = module.postgresql.db_host
  }

  variable {
    name         = "DATABASE_USER"
    value        = "copilotdatabase"
    # value        = module.postgresql.db_user
  }

  variable {
    name         = "DATABASE_PORT"
    value        = "5432"
    # value        = tostring(module.postgresql.db_port)
  }

  variable {
    name         = "DATABASE_NAME"
    value        = "postgres"
    # value        = module.postgresql.db_name
  }

  variable {
    name         = "DATABASE_PASSWORD"
    secret_value = var.db_password
    # secret_value = random_password.db_admin.result
    is_secret    = true
  }

  variable {
    name         = "REDIS_HOST"
    value        = module.redis.hostname
  }

  variable {
    name         = "REDIS_PORT"
    value        = tostring(module.redis.port)
  }

  variable {
    name         = "REDIS_PASSWORD"
    secret_value = module.redis.primary_access_key
    is_secret    = true
  }

  variable {
    name         = "AZURE_OPENAI_ENDPOINT"
    value        = "https://nikhi-ma0k8gv3-eastus2.cognitiveservices.azure.com/"
  }

  variable {
    name         = "AZURE_OPENAI_API_KEY"
    secret_value = var.azure_openai_key
    is_secret    = true
  }

  variable {
    name         = "AZURE_EMBEDDER_OPENAI_ENDPOINT"
    value        = "https://nikhi-ma0k8gv3-eastus2.cognitiveservices.azure.com/"
  }
  
  variable {
    name         = "AZURE_EMBEDDER_DEPLOYMENT"
    value        = "text-embedding-3-large"
  }

  variable {
    name         = "AZURE_EMBEDDER_OPENAI_API_KEY"
    secret_value = var.azure_openai_key
    is_secret    = true
  }

}
