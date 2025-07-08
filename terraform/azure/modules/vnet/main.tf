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
  depends_on = [
    var.private_subnet_dependencies
  ]
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

# Associate NAT Gateway with all private subnets for internet egress
resource "azurerm_subnet_nat_gateway_association" "private" {
  for_each       = azurerm_subnet.private
  subnet_id      = each.value.id
  nat_gateway_id = azurerm_nat_gateway.this.id
}

# Network Security Group for AKS private subnets
resource "azurerm_network_security_group" "aks_private" {
  name                = "${var.vnet_name}-aks-private-nsg"
  location            = var.location
  resource_group_name = var.resource_group_name

  security_rule {
    name                       = "Allow-HTTP-Inbound"
    priority                   = 1001
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "80"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
    description                = "Allow inbound HTTP traffic to AKS nodes"
  }
}

# Associate NSG with all private subnets
resource "azurerm_subnet_network_security_group_association" "private" {
  for_each                  = azurerm_subnet.private
  subnet_id                 = each.value.id
  network_security_group_id = azurerm_network_security_group.aks_private.id
}
