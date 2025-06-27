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

# Add NAT Gateway and Public IP for Internet egress from private subnet
resource "azurerm_public_ip" "nat_gw" {
  name                = "${var.vnet_name}-nat-gw-pip"
  resource_group_name = var.resource_group_name
  location            = var.location
  allocation_method   = "Static"
  sku                 = "Standard"
}

resource "azurerm_nat_gateway" "this" {
  name                = "${var.vnet_name}-nat-gw"
  resource_group_name = var.resource_group_name
  location            = var.location
  sku_name            = "Standard"
}

resource "azurerm_nat_gateway_public_ip_association" "this" {
  nat_gateway_id       = azurerm_nat_gateway.this.id
  public_ip_address_id = azurerm_public_ip.nat_gw.id
}

# Associate NAT Gateway with the first private subnet (AKS nodes)
resource "azurerm_subnet_nat_gateway_association" "private_aks" {
  subnet_id      = azurerm_subnet.private["0"].id
  nat_gateway_id = azurerm_nat_gateway.this.id
}