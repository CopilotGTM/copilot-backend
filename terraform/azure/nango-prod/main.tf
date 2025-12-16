# Phase 0: Foundations — Resource Group and Virtual Network

module "resource_group" {
  source              = "../modules/resource_group"
  resource_group_name = var.resource_group_name
  location            = var.location
}

module "vnet" {
  source                     = "../modules/vnet"
  vnet_name                  = var.vnet_name
  address_space              = var.vnet_address_space
  location                   = var.location
  resource_group_name        = module.resource_group.resource_group_name
  public_subnets             = var.public_subnets
  private_subnets            = var.private_subnets
  env                        = var.env
  private_subnet_dependencies = []

  depends_on = [module.resource_group]
}
