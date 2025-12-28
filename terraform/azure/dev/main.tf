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
  source                          = "../modules/vnet"
  vnet_name                       = "${var.vnet_name}-${var.env}"
  address_space                   = var.vnet_address_space
  location                        = module.resource_group.resource_group_location
  resource_group_name             = module.resource_group.resource_group_name
  public_subnets                  = var.vnet_public_subnets
  private_subnets                 = var.vnet_private_subnets
  private_endpoints_subnet_prefix = var.vnet_private_endpoints_subnet
  waf_subnet_prefix               = var.vnet_waf_subnet
  env                             = var.env
  depends_on = [
    module.resource_group
  ]
}

# -----------------------------------------------------------------
# Application Gateway (WAF)
# -----------------------------------------------------------------
/*
module "waf" {
  source              = "../modules/waf"
  waf_name            = "${var.waf_name}-${var.env}"
  resource_group_name = module.resource_group.resource_group_name
  location            = var.location
  subnet_id           = module.vnet.waf_subnet_id
  
  tags = {
    environment = var.env
    project     = var.project_name
  }
  depends_on = [
    module.vnet
  ]
}
*/

# -----------------------------------------------------------------
# Azure Redis Cache: In-memory data store for caching and sessions
# -----------------------------------------------------------------
/*
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
*/

data "azurerm_client_config" "current" {}

# AzureAD client config (for tenant diagnostic of workload identity)
data "azuread_client_config" "current" {}

# -----------------------------------------------------------------
# Private DNS: Key Vault Private Link
# Created once per environment; individual Key Vaults attach via zone groups.
# -----------------------------------------------------------------
/*
resource "azurerm_private_dns_zone" "key_vault" {
  count               = var.enable_key_vault_private_endpoints ? 1 : 0
  name                = "privatelink.vaultcore.azure.net"
  resource_group_name = module.resource_group.resource_group_name
  tags = {
    environment = var.env
    project     = var.project_name
  }
}

resource "azurerm_private_dns_zone_virtual_network_link" "key_vault" {
  count                 = var.enable_key_vault_private_endpoints ? 1 : 0
  name                  = "kv-${var.env}-vnet-link"
  resource_group_name   = module.resource_group.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.key_vault[0].name
  virtual_network_id    = module.vnet.vnet_id
  registration_enabled  = false
}
*/

# -----------------------------------------------------------------
# Private DNS: Storage Account (Blob) Private Link
# -----------------------------------------------------------------
/*
resource "azurerm_private_dns_zone" "storage_blob" {
  count               = var.enable_storage_private_endpoints ? 1 : 0
  name                = "privatelink.blob.core.windows.net"
  resource_group_name = module.resource_group.resource_group_name
  tags = {
    environment = var.env
    project     = var.project_name
  }
}

resource "azurerm_private_dns_zone_virtual_network_link" "storage_blob" {
  count                 = var.enable_storage_private_endpoints ? 1 : 0
  name                  = "stblob-${var.env}-vnet-link"
  resource_group_name   = module.resource_group.resource_group_name
  private_dns_zone_name = azurerm_private_dns_zone.storage_blob[0].name
  virtual_network_id    = module.vnet.vnet_id
  registration_enabled  = false
}
*/

# -----------------------------------------------------------------
# Azure Key Vault
# -----------------------------------------------------------------
/*
module "database_key_vault" {
  source                        = "../modules/key_vault"
  key_vault_name                = "copilot-gtm-database-kv-${var.env}"
  location                      = var.location
  resource_group_name           = module.resource_group.resource_group_name
  tenant_id                     = data.azurerm_client_config.current.tenant_id
  admin_object_id               = data.azurerm_client_config.current.object_id
  public_network_access_enabled = var.enable_key_vault_private_endpoints ? false : true
  private_endpoint_enabled      = var.enable_key_vault_private_endpoints
  private_endpoint_subnet_id    = module.vnet.private_endpoints_subnet_id
  private_dns_zone_id           = var.enable_key_vault_private_endpoints ? azurerm_private_dns_zone.key_vault[0].id : null
  tags = {
    environment = var.env
    project     = var.project_name
  }
  depends_on = [
    module.resource_group
  ]
}
*/

# -----------------------------------------------------------------
# Azure Key Vault
# -----------------------------------------------------------------
/*
module "master_key_vault" {
  source                        = "../modules/key_vault"
  key_vault_name                = "copilot-gtm-master-kv-${var.env}"
  location                      = var.location
  resource_group_name           = module.resource_group.resource_group_name
  tenant_id                     = data.azurerm_client_config.current.tenant_id
  admin_object_id               = data.azurerm_client_config.current.object_id
  public_network_access_enabled = var.enable_key_vault_private_endpoints ? false : true
  private_endpoint_enabled      = var.enable_key_vault_private_endpoints
  private_endpoint_subnet_id    = module.vnet.private_endpoints_subnet_id
  private_dns_zone_id           = var.enable_key_vault_private_endpoints ? azurerm_private_dns_zone.key_vault[0].id : null
  tags = {
    environment = var.env
    project     = var.project_name
  }
  depends_on = [
    module.resource_group
  ]
}
*/

# -----------------------------------------------------------------
# Azure Storage Account: General purpose blob/file storage
# -----------------------------------------------------------------
/*
module "master_storage_account" {
  source                             = "../modules/storage_account"
  storage_account_name               = "${var.storage_account_name}${var.env}"
  resource_group_name                = module.resource_group.resource_group_name
  location                           = var.location
  account_tier                       = "Standard"
  account_replication_type           = "LRS"
  public_network_access_enabled      = var.enable_storage_private_endpoints ? false : true
  private_endpoint_enabled           = var.enable_storage_private_endpoints
  private_endpoint_subnet_id         = module.vnet.private_endpoints_subnet_id
  private_endpoint_subresource_names = ["blob"]
  private_dns_zone_ids               = var.enable_storage_private_endpoints ? [azurerm_private_dns_zone.storage_blob[0].id] : null
  allowed_blob_cors_origins = [
    "https://${var.frontend_dns_a_record_name}.copilotgtm.com",
    "https://${var.frontend_admin_dns_a_record_name}.copilotgtm.com",
    "http://localhost:3000"
  ]
  tags = {
    environment = var.env
    project     = var.project_name
  }
}
*/

# -----------------------------------------------------------------
# Organisation Data Storage Account: dedicated per-organisation data store
# -----------------------------------------------------------------
/*
module "org_storage_account" {
  source                             = "../modules/storage_account"
  storage_account_name               = "${var.org_storage_account_name}${var.env}"
  resource_group_name                = module.resource_group.resource_group_name
  location                           = var.location
  account_tier                       = "Standard"
  account_replication_type           = "LRS"
  public_network_access_enabled      = var.enable_storage_private_endpoints ? false : true
  private_endpoint_enabled           = var.enable_storage_private_endpoints
  private_endpoint_subnet_id         = module.vnet.private_endpoints_subnet_id
  private_endpoint_subresource_names = ["blob"]
  private_dns_zone_ids               = var.enable_storage_private_endpoints ? [azurerm_private_dns_zone.storage_blob[0].id] : null
  allowed_blob_cors_origins = [
    "https://${var.frontend_dns_a_record_name}.copilotgtm.com",
    "https://${var.frontend_admin_dns_a_record_name}.copilotgtm.com",
    "http://localhost:3000"
  ]
  tags = {
    environment = var.env
    project     = var.project_name
    purpose     = "organisation-data"
  }
}
*/

# -----------------------------------------------------------------
# Azure Container Registry: Stores container images for AKS and DevOps pipelines
# -----------------------------------------------------------------
/*
module "acr" {
  source              = "../modules/acr"
  acr_name            = "${var.acr_name}${var.env}"
  resource_group_name = module.resource_group.resource_group_name
  location            = var.location
  sku                 = var.acr_type
  subnet_id           = module.vnet.private_subnet_ids[0]
  vnet_id             = module.vnet.vnet_id
  depends_on = [
    module.resource_group,
    module.vnet
  ]
}
*/

# -----------------------------------------------------------------
# Azure Kubernetes Service: Managed Kubernetes cluster for containers
# -----------------------------------------------------------------
/*
module "aks" {
  source                 = "../modules/aks"
  aks_name               = "${var.aks_name}-${var.env}"
  dns_prefix             = "${var.aks_dns_prefix}-${var.env}"
  location               = module.resource_group.resource_group_location
  resource_group_name    = module.resource_group.resource_group_name
  vnet_subnet_ids        = module.vnet.private_subnet_ids
  agent_count            = var.aks_node_count
  vm_size                = var.aks_vm_size
  os_disk_size_gb        = var.aks_os_disk_size_gb
  service_cidr           = var.aks_service_cidr
  dns_service_ip         = var.aks_dns_service_ip
  env                    = var.env
  enable_gpu_worker_pool = var.enable_gpu_worker_pool
  gpu_node_pool_name     = var.gpu_node_pool_name
  gpu_vm_size            = var.gpu_vm_size
  gpu_min_count          = var.gpu_min_count
  gpu_max_count          = var.gpu_max_count
  gpu_node_labels        = var.gpu_node_labels
  gpu_node_taints        = var.gpu_node_taints
  
  # Enable AGIC
  app_gateway_id         = module.waf.waf_id

  depends_on = [
    module.vnet,
    module.acr,
    module.waf
  ]
}
*/



# -----------------------------------------------------------------
# Role Assignment: Allow AKS kubelet to pull images from ACR
# -----------------------------------------------------------------
/*
resource "azurerm_role_assignment" "aks_kubelet_acr_pull" {
  principal_id                     = module.aks.kubelet_identity_object_id
  role_definition_name             = "AcrPull"
  scope                            = module.acr.acr_id
  skip_service_principal_aad_check = true
  depends_on = [
    module.acr,
    module.aks
  ]
}
*/

# -----------------------------------------------------------------
# Workload Identity (AAD application + federated identity for ServiceAccount)
# Replaces UAMI + federated identity credential resources
# -----------------------------------------------------------------
/*
module "workload_identity_backend" {
  source                    = "../modules/workload_identity"
  name                      = "copilot-backend-wi-${var.env}"
  oidc_issuer_url           = module.aks.oidc_issuer_url
  service_account_namespace = var.aks_main_app_namespace
  service_account_name      = var.aks_main_app_service_account
  create_password           = false
  depends_on = [
    module.aks
  ]
}
*/

# Separate workload identity for workers (shared ServiceAccount for all workers)
/*
module "workload_identity_workers" {
  source                    = "../modules/workload_identity"
  name                      = "copilot-workers-wi-${var.env}"
  oidc_issuer_url           = module.aks.oidc_issuer_url
  service_account_namespace = var.aks_main_app_namespace
  service_account_name      = var.aks_workers_service_account
  create_password           = false
  depends_on = [
    module.aks
  ]
}
*/

# Role assignments for workload identity SP (least privilege scopes similar to prior UAMI)
/*
resource "azurerm_role_assignment" "wi_kv_database_secrets_user" {
  scope                = module.database_key_vault.key_vault_id
  role_definition_name = "Key Vault Secrets Officer" # upgraded from Secrets User for create/update/delete
  principal_id         = module.workload_identity_backend.object_id
}

resource "azurerm_role_assignment" "wi_kv_master_secrets_user" {
  scope                = module.master_key_vault.key_vault_id
  role_definition_name = "Key Vault Secrets Officer" # upgraded from Secrets User for create/update/delete
  principal_id         = module.workload_identity_backend.object_id
}

resource "azurerm_role_assignment" "wi_master_storage_blob_contributor" {
  scope                = module.master_storage_account.storage_account_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = module.workload_identity_backend.object_id
}

resource "azurerm_role_assignment" "wi_master_storage_blob_delegator" {
  scope                = module.master_storage_account.storage_account_id
  role_definition_name = "Storage Blob Delegator"
  principal_id         = module.workload_identity_backend.object_id
}

resource "azurerm_role_assignment" "wi_org_storage_blob_contributor" {
  scope                = module.org_storage_account.storage_account_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = module.workload_identity_backend.object_id
}

resource "azurerm_role_assignment" "wi_org_storage_blob_delegator" {
  scope                = module.org_storage_account.storage_account_id
  role_definition_name = "Storage Blob Delegator"
  principal_id         = module.workload_identity_backend.object_id
}

# Workers identity role assignments (mirror backend access for now)
resource "azurerm_role_assignment" "wi_workers_kv_database_secrets_user" {
  scope                = module.database_key_vault.key_vault_id
  role_definition_name = "Key Vault Secrets Officer" # upgraded for write/delete
  principal_id         = module.workload_identity_workers.object_id
}

resource "azurerm_role_assignment" "wi_workers_kv_master_secrets_user" {
  scope                = module.master_key_vault.key_vault_id
  role_definition_name = "Key Vault Secrets Officer" # upgraded for write/delete
  principal_id         = module.workload_identity_workers.object_id
}

resource "azurerm_role_assignment" "wi_workers_master_storage_blob_contributor" {
  scope                = module.master_storage_account.storage_account_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = module.workload_identity_workers.object_id
}

resource "azurerm_role_assignment" "wi_workers_master_storage_blob_delegator" {
  scope                = module.master_storage_account.storage_account_id
  role_definition_name = "Storage Blob Delegator"
  principal_id         = module.workload_identity_workers.object_id
}

resource "azurerm_role_assignment" "wi_workers_org_storage_blob_contributor" {
  scope                = module.org_storage_account.storage_account_id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = module.workload_identity_workers.object_id
}

resource "azurerm_role_assignment" "wi_workers_org_storage_blob_delegator" {
  scope                = module.org_storage_account.storage_account_id
  role_definition_name = "Storage Blob Delegator"
  principal_id         = module.workload_identity_workers.object_id
}
*/

# -----------------------------------------------------------------
# Azure AI Foundry
# -----------------------------------------------------------------
/*
module "foundry" {
  source              = "../modules/foundry"
  prefix              = "foundry"
  resource_group_name = module.resource_group.resource_group_name
  resource_group_id   = module.resource_group.resource_group_id
  location            = "eastus2" # OpenAI models are available in East US 2
  use_openai          = true
  depends_on = [
    module.resource_group
  ]
}
*/

# -----------------------------------------------------------------
# Backend DEVELOPMENT Identity - Permissions provided via AKS kubelet identity  
# The aks_* resources provide the necessary permissions for pods to access
# Key Vault secrets and storage accounts via the kubelet identity.
# No additional backend_* resources needed as they would be duplicates.
# -----------------------------------------------------------------

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
/*
module "application_database" {
  source              = "../modules/postgresql"
  env                 = var.env
  resource_group_name = module.resource_group.resource_group_name
  location            = var.db_location
  db_name             = var.db_name
  db_admin_user       = "CopilotGTMAdmin"
  db_admin_password   = random_password.db_admin.result
  key_vault_id        = module.database_key_vault.key_vault_id
  tags = {
    environment = var.env
    project     = var.project_name
  }
  depends_on = [
    module.resource_group,
    module.database_key_vault
  ]
}
*/

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
  project_id  = data.azuredevops_project.project.id
  name        = "app-secrets-${var.env}"
  description = "Service-principal creds and other secrets for ${var.env}"

  variable {
    name  = "ENV"
    value = "dev"
  }

  variable {
    name  = "CLOUD_PROVIDER"
    value = "azure"
  }

  variable {
    name  = "APPLICATION_REGION"
    value = module.resource_group.resource_group_location
  }

  variable {
    name  = "AZURE_RESOURCE_GROUP"
    value = module.resource_group.resource_group_name
  }

  variable {
    name  = "APPLICATION_STORAGE_NAME"
    value = "placeholder" # module.master_storage_account.storage_account_name
  }

  variable {
    name  = "ORGANISATION_STORAGE_NAME"
    value = "placeholder" # module.org_storage_account.storage_account_name
  }

  variable {
    name  = "AZURE_KEY_VAULT_URL"
    value = "placeholder" # module.database_key_vault.key_vault_uri
  }

  variable {
    name  = "AZURE_SUBSCRIPTION_ID"
    value = var.subscription_id
  }
  variable {
    name  = "DATABASE_HOST"
    value = "placeholder" # module.application_database.db_host
  }

  variable {
    name  = "DATABASE_USER"
    value = "placeholder" # module.application_database.db_user
  }

  variable {
    name  = "DATABASE_PORT"
    value = "5432" # tostring(module.application_database.db_port)
  }

  variable {
    name  = "DATABASE_NAME"
    value = "placeholder" # module.application_database.db_name
  }

  variable {
    name         = "DATABASE_PASSWORD"
    secret_value = random_password.db_admin.result
    is_secret    = true
  }

  variable {
    name         = "AGENT_PASSWORD"
    secret_value = random_password.agent_admin.result
    is_secret    = true
  }

  variable {
    name  = "DATABASE_SSL_MODE"
    value = "prefer"
  }

  variable {
    name  = "REDIS_HOST"
    value = "placeholder" # module.redis.hostname
  }

  variable {
    name  = "REDIS_PORT"
    value = "6379" # tostring(module.redis.port)
  }

  variable {
    name         = "REDIS_PASSWORD"
    secret_value = "placeholder" # module.redis.primary_access_key
    is_secret    = true
  }

  variable {
    name  = "AZURE_OPENAI_ENDPOINT"
    value = "placeholder" # module.foundry.openai_endpoint_url
  }

  variable {
    name         = "AZURE_OPENAI_API_KEY"
    secret_value = var.azure_openai_key
    is_secret    = true
  }

  variable {
    name  = "AZURE_EMBEDDER_OPENAI_ENDPOINT"
    value = "placeholder" # module.foundry.openai_endpoint_url
  }

  variable {
    name  = "AZURE_EMBEDDER_DEPLOYMENT"
    value = "placeholder" # module.foundry.embedder_deployment_name
  }

  variable {
    name         = "AZURE_EMBEDDER_OPENAI_API_KEY"
    secret_value = var.azure_openai_key
    is_secret    = true
  }

  variable {
    name  = "SUPER_ADMIN_EMAIL"
    value = var.super_admin_email
  }

  variable {
    name  = "SUPABASE_URL"
    value = var.supabase_url
  }

  variable {
    name         = "SUPABASE_ANON_KEY"
    secret_value = var.supabase_anon_key
    is_secret    = true
  }

  variable {
    name         = "SUPABASE_SERVICE_ROLE_KEY"
    secret_value = var.supabase_service_role_key
    is_secret    = true
  }

  variable {
    name         = "SUPABASE_JWT_SECRET"
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
    name  = "ADMIN_APP_URL"
    value = "https://${var.frontend_admin_dns_a_record_name}.copilotgtm.com"
  }

  variable {
    name  = "FROM_EMAIL"
    value = var.resend_email
  }

  variable {
    name         = "RESEND_API_KEY"
    secret_value = var.resend_api_key
    is_secret    = true
  }

  variable {
    name  = "WEB_APP_NAME"
    value = "placeholder" # module.frontend_app_service.app_service_name
  }

  variable {
    name  = "RESOURCE_GROUP"
    value = module.resource_group.resource_group_name
  }

  # Workload Identity client IDs (for Helm ServiceAccount annotations)
  variable {
    name  = "WORKLOAD_IDENTITY_BACKEND_CLIENT_ID"
    value = "placeholder" # module.workload_identity_backend.client_id
  }

  variable {
    name  = "WORKLOAD_IDENTITY_WORKERS_CLIENT_ID"
    value = "placeholder" # module.workload_identity_workers.client_id
  }

  # Workload Identity tenant IDs (for Azure SDK authentication)
  variable {
    name  = "WORKLOAD_IDENTITY_API_TENANT_ID"
    value = data.azuread_client_config.current.tenant_id
  }

  variable {
    name  = "WORKLOAD_IDENTITY_WORKERS_TENANT_ID"
    value = data.azuread_client_config.current.tenant_id
  }

  variable {
    name         = "NANGO_SECRET_KEY"
    secret_value = var.nango_secret
    is_secret    = true
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
/*
module "service_connection_acr" {
  source                = "../modules/service_connection_acr"
  devops_project_id     = data.azuredevops_project.project.id
  service_endpoint_name = "copilotgtm-acr-connection-${var.env}"
  acr_login_server      = module.acr.acr_login_server
  acr_sp_app_id         = module.service_principal.client_id
  acr_sp_app_secret     = module.service_principal.client_secret
}
*/

resource "azuredevops_pipeline_authorization" "service_connection_authorization" {
  project_id  = data.azuredevops_project.project.id
  resource_id = module.service_connection.service_connection_id
  type        = "endpoint"
}

/*
resource "azuredevops_pipeline_authorization" "service_connection_acr_authorization" {
  project_id  = data.azuredevops_project.project.id
  resource_id = module.service_connection_acr.acr_service_connection_id
  type        = "endpoint"
}
*/

# -----------------------------------------------------------------
# Test Resource: Simple resource to verify pipeline execution
# -----------------------------------------------------------------
resource "random_string" "test_resource" {
  length  = 8
  special = false
  upper   = false
}

output "test_resource_value" {
  value = random_string.test_resource.result
}
resource "random_password" "agent_admin" {
  length           = 32
  override_special = "!@#$%&*()-_=+[]"
  special          = true
  upper            = true
  lower            = true
  numeric          = true
}

# -----------------------------------------------------------------
# Self-Hosted DevOps Agent VM
# Placed in private subnet to access Key Vault/Storage via Private Link
# -----------------------------------------------------------------
module "devops_agent" {
  source              = "../modules/devops_agent"
  resource_group_name = module.resource_group.resource_group_name
  location            = var.location
  vm_name             = "${var.agent_name}-${var.env}"
  vm_size             = "Standard_D2as_v4"
  admin_username      = var.admin_username
  admin_password      = random_password.agent_admin.result

  # Place in private subnet for Private Link access
  subnet_id = module.vnet.private_subnet_ids[0]

  # Azure DevOps configuration
  azuredevops_url = var.org_service_url
  azuredevops_pat = local.effective_azuredevops_pat
  agent_pool      = var.agent_pool
  agent_name      = var.agent_name

  # Pre-install tools for CI/CD pipelines
  install_docker    = true
  install_terraform = true
  terraform_version = "1.9.0"
  install_kubectl   = true
  install_azure_cli = true

  # No public IP needed - agent communicates outbound only
  enable_public_ip = true

  tags = {
    environment = var.env
    project     = var.project_name
    purpose     = "azure-devops-agent"
  }

  depends_on = [
    module.vnet,
    module.resource_group
  ]
}
