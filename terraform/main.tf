module "resource_group" {
  source              = "./modules/resource_group"
  resource_group_name = "${var.resource_group_name}-${var.env}"
  location            = var.location
}

module "vnet" {
  source              = "./modules/vnet"
  vnet_name           = "vnet-${var.env}"
  address_space       = var.vnet_address_space
  location            = module.resource_group.resource_group_location
  resource_group_name = module.resource_group.resource_group_name
  public_subnets      = var.vnet_public_subnets
  private_subnets     = var.vnet_private_subnets
  env                 = var.env
}

module "aks" {
  source               = "./modules/aks"
  aks_name             = var.aks_name
  dns_prefix           = var.aks_dns_prefix
  location             = module.resource_group.resource_group_location
  resource_group_name  = module.resource_group.resource_group_name
  subnet_id            = module.vnet.private_subnet_ids[0]
  agent_count          = var.aks_node_count
  vm_size              = var.aks_vm_size
  service_cidr         = var.aks_service_cidr
  dns_service_ip       = var.aks_dns_service_ip
  env                  = var.env
}
module "acr" {
  source              = "./modules/acr"
  acr_name            = "copilottechnicaldeck"
  resource_group_name = module.resource_group.resource_group_name
  location            = var.location
  sku                 = "Basic"
  admin_enabled       = false
}
resource "azurerm_role_assignment" "aks_kubelet_acr_pull" {
  principal_id         = "183d37e0-c2d9-4c51-b6e2-2e5b2e996f4d"
  role_definition_name = "AcrPull"
  scope                = module.acr.acr_id
}

resource "azurerm_role_assignment" "aks_acr_pull" {
  principal_id         = module.aks.aks_principal_id
  role_definition_name = "AcrPull"
  scope                = module.acr.acr_id
}

module "redis" {
  source              = "./modules/redis"
  redis_name          = "copilotredis${random_integer.suffix.result}"
  resource_group_name = module.resource_group.resource_group_name
  location            = var.location
  capacity            = 0
  family              = "C"
  tags = {
    environment = var.env
    project     = "copilot"
  }
}

module "frontend_app_service" {
  source                  = "./modules/app_service"
  app_service_plan_name   = "frontend-asp-${var.env}"
  app_service_name        = "copilot-frontend-${var.env}"
  resource_group_name     = module.resource_group.resource_group_name
  location                = var.location
  node_version            = "20-lts"
  app_settings            = {}
}

resource "random_integer" "suffix" {
  min = 10000
  max = 99999
}
