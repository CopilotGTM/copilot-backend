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

data "azurerm_client_config" "current" {}

# -----------------------------------------------------------------
# Azure Key Vault
# -----------------------------------------------------------------
module "database_key_vault" {
  source              = "../modules/key_vault"
  key_vault_name      = "app-database-vault-${var.env}"
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

# -----------------------------------------------------------------
# Azure Key Vault
# -----------------------------------------------------------------
module "master_key_vault" {
  source              = "../modules/key_vault"
  key_vault_name      = "app-master-vault-${var.env}"
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

# -----------------------------------------------------------------
# Azure Storage Account: General purpose blob/file storage
# -----------------------------------------------------------------
module "storage_account" {
  source                  = "../modules/storage_account"
  storage_account_name    = "${var.storage_account_name}${var.env}"
  resource_group_name     = module.resource_group.resource_group_name
  location                = var.location
  account_tier            = "Standard"
  account_replication_type = "LRS"
  tags = {
    environment = var.env
    project     = var.project_name
  }
}

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
  vnet_subnet_ids      = module.vnet.private_subnet_ids
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
# Azure AI Foundry
# -----------------------------------------------------------------
module "foundry" {
  source              = "../modules/foundry"
  prefix              = "foundry"
  resource_group_name = module.resource_group.resource_group_name
  resource_group_id   = module.resource_group.resource_group_id
  location            = var.location
  use_openai          = true
  depends_on = [
    module.resource_group
  ]
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

# -----------------------------------------------------------------
# PostgreSQL Flexible Server
# -----------------------------------------------------------------
module "application_database" {
  source                   = "../modules/postgresql"
  env                      = var.env
  resource_group_name      = module.resource_group.resource_group_name
  location                 = "westus"
  db_admin_user            = "CopilotGTMAdmin"
  db_admin_password        = random_password.db_admin.result
  key_vault_id             = module.database_key_vault.key_vault_id
  tags                     = {
    environment = var.env
    project     = var.project_name
  }
  depends_on = [
    module.resource_group,
    module.database_key_vault
  ]
}

# # -----------------------------------------------------------------
# # Create the DNS CNAME record pointing at the frontend app service
# # -----------------------------------------------------------------
# resource "azurerm_dns_cname_record" "frontend_app" {
#   name                = var.frontend_dns_a_record_name
#   zone_name           = var.dns_zone_name
#   resource_group_name = var.dns_zone_resource_group
#   ttl                 = 300
#   record              = module.frontend_app_service.app_service_default_hostname
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
    value        = module.resource_group.resource_group_location
  }

  variable {
    name         = "AZURE_RESOURCE_GROUP"
    value        = module.resource_group.resource_group_name
  }

  variable {
    name         = "APPLICATION_STORAGE_NAME"
    value        = module.storage_account.storage_account_name
  }

  variable {
    name        = "AZURE_KEY_VAULT_URL"
    value        = module.database_key_vault.key_vault_uri
  }

  variable {
    name         = "AZURE_SUBSCRIPTION_ID"
    value        = var.subscription_id
  }
  variable {
    name         = "DATABASE_HOST"
    # value        = "dev-copilot-gtm.postgres.database.azure.com"
    value        = module.application_database.db_host
  }

  variable {
    name         = "DATABASE_USER"
    # value        = "copilotdatabase"
    value        = module.application_database.db_user
  }

  variable {
    name         = "DATABASE_PORT"
    # value        = "5432"
    value        = tostring(module.application_database.db_port)
  }

  variable {
    name         = "DATABASE_NAME"
    # value        = "postgres"
    value        = module.application_database.db_name
  }

  variable {
    name         = "DATABASE_PASSWORD"
    # secret_value = var.db_password
    secret_value = random_password.db_admin.result
    is_secret    = true
  }

  variable {
    name         = "DATABASE_SSL_MODE"
    value = "prefer"
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
    value        = module.foundry.openai_endpoint_url
  }

  variable {
    name         = "AZURE_OPENAI_API_KEY"
    secret_value = var.azure_openai_key
    is_secret    = true
  }

  variable {
    name         = "AZURE_EMBEDDER_OPENAI_ENDPOINT"
    value        = module.foundry.openai_endpoint_url
  }
  
  variable {
    name         = "AZURE_EMBEDDER_DEPLOYMENT"
    value        = module.foundry.embedder_deployment_name
  }

  variable {
    name         = "AZURE_EMBEDDER_OPENAI_API_KEY"
    secret_value = var.azure_openai_key
    is_secret    = true
  }

  variable {
    name = "SUPER_ADMIN_EMAIL"
    value = var.super_admin_email
  }

  variable {
    name  = "SUPABASE_URL"
    value = var.supabase_url
  }

  variable {
    name  = "SUPABASE_ANON_KEY"
    secret_value = var.supabase_anon_key
    is_secret    = true
  }

  variable {
    name  = "SUPABASE_SERVICE_ROLE_KEY"
    secret_value = var.supabase_service_role_key
    is_secret    = true
  }

  variable {
    name  = "SUPABASE_JWT_SECRET"
    secret_value = var.supabase_jwt_secret
    is_secret    = true
  }

  variable {
    name  = "AZURE_SUBSCRIPTION"
    value = module.service_connection.service_connection_name
  }

  variable {
    name  = "APP_NAME"
    value = "CopilotGTM"
  }

  variable {
    name  = "APP_URL"
    value = "https://${var.frontend_dns_a_record_name}.copilotgtm.com"
  }

  variable {
    name = "FROM_EMAIL"
    value = var.resend_email
  }

  variable {
    name  = "RESEND_API_KEY"
    secret_value = var.resend_api_key
    is_secret    = true
  }

  variable {
    name  = "WEB_APP_NAME"
    value = module.frontend_app_service.app_service_name
  }

  variable {
    name  = "RESOURCE_GROUP"
    value = module.resource_group.resource_group_name
  }
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
  source                  = "../modules/service_connection"
  devops_project_id       = data.azuredevops_project.project.id
  service_connection_name = "copilotgtm-arc-ci-cd-service--${var.env}"
  subscription_id         = var.subscription_id
  subscription_name       = "Copilot GTM Subscription"
  tenant_id               = module.service_principal.tenant_id
  client_id               = module.service_principal.client_id
  client_secret           = module.service_principal.client_secret
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

resource "azuredevops_resource_authorization" "service_connection_authorization" {
  project_id  = data.azuredevops_project.project.id
  resource_id = module.service_connection.service_connection_id
  authorized  = true
  type        = "endpoint"
}

resource "azuredevops_resource_authorization" "service_connection_acr_authorization" {
  project_id  = data.azuredevops_project.project.id
  resource_id = module.service_connection_acr.acr_service_connection_id
  authorized  = true
  type        = "endpoint"
}
