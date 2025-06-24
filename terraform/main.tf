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

resource "random_integer" "suffix" {
  min = 10000
  max = 99999
}
