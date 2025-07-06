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
  depends_on          = [module.resource_group]
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
  depends_on           = [module.vnet]
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
  depends_on          = [module.resource_group]
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
  depends_on          = [module.resource_group]
}

# -----------------------------------------------------------------
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
  depends_on              = [module.resource_group]
}

# -----------------------------------------------------------------
# Role Assignment: Allow AKS kubelet to pull images from ACR
# -----------------------------------------------------------------
resource "azurerm_role_assignment" "aks_kubelet_acr_pull" {
  principal_id         = module.aks.aks_principal_id
  role_definition_name = "AcrPull"
  scope                = module.acr.acr_id
  principal_type       = "ServicePrincipal"
}

# -----------------------------------------------------------------
# Role Assignment: Allow App Service to pull images from ACR
# -----------------------------------------------------------------
resource "azurerm_role_assignment" "app_service_acr_pull" {
  principal_id         = module.frontend_app_service.app_service_principal_id
  role_definition_name = "AcrPull"
  scope                = module.acr.acr_id
  principal_type       = "ServicePrincipal"
  depends_on           = [module.frontend_app_service.app_service_id]
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
