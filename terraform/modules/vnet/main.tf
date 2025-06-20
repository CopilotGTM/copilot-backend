resource "azurerm_virtual_network" "this" {
  name                = var.vnet_name
  address_space       = var.address_space
  location            = var.location
  resource_group_name = var.resource_group_name

  tags = {
    env = var.env
  }
}

resource "azurerm_subnet" "public" {
  for_each             = { for idx, prefix in var.public_subnets : idx => prefix }
  name                 = "${var.vnet_name}-public-${each.key + 1}"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [each.value]
}

resource "azurerm_subnet" "private" {
  for_each             = { for idx, prefix in var.private_subnets : idx => prefix }
  name                 = "${var.vnet_name}-private-${each.key + 1}"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [each.value]
}