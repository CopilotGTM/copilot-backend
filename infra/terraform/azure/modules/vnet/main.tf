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

resource "azurerm_subnet" "private_endpoints" {
  count                = var.private_endpoints_subnet_prefix != "" ? 1 : 0
  name                 = "${var.vnet_name}-private-endpoints"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [var.private_endpoints_subnet_prefix]

  private_endpoint_network_policies = "Disabled"
}

resource "azurerm_subnet" "managed_devops_pools" {
  for_each             = { for subnet in var.managed_devops_pools_subnets : subnet.name => subnet }
  name                 = "${var.vnet_name}-${each.value.name}"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [each.value.address_prefix]

  delegation {
    name = "managed-devops-pools"

    service_delegation {
      name = "Microsoft.DevOpsInfrastructure/pools"
      actions = [
        "Microsoft.Network/virtualNetworks/subnets/join/action",
      ]
    }
  }
}

# Add NAT Gateway and Public IP when deterministic outbound egress is required.
resource "azurerm_public_ip" "nat_gw" {
  count               = var.enable_nat_gateway ? 1 : 0
  name                = "${var.vnet_name}-nat-gw-pip"
  resource_group_name = var.resource_group_name
  location            = var.location
  allocation_method   = "Static"
  sku                 = "Standard"
}

resource "azurerm_nat_gateway" "this" {
  count               = var.enable_nat_gateway ? 1 : 0
  name                = "${var.vnet_name}-nat-gw"
  resource_group_name = var.resource_group_name
  location            = var.location
  sku_name            = "Standard"
}

resource "azurerm_nat_gateway_public_ip_association" "this" {
  count                = var.enable_nat_gateway ? 1 : 0
  nat_gateway_id       = azurerm_nat_gateway.this[0].id
  public_ip_address_id = azurerm_public_ip.nat_gw[0].id
}

# Associate NAT Gateway with all private subnets for internet egress
resource "azurerm_subnet_nat_gateway_association" "private" {
  for_each       = var.enable_nat_gateway ? azurerm_subnet.private : {}
  subnet_id      = each.value.id
  nat_gateway_id = azurerm_nat_gateway.this[0].id
}

resource "azurerm_subnet_nat_gateway_association" "managed_devops_pools" {
  for_each       = var.enable_nat_gateway ? azurerm_subnet.managed_devops_pools : {}
  subnet_id      = each.value.id
  nat_gateway_id = azurerm_nat_gateway.this[0].id
}

# Network Security Group for AKS private subnets
resource "azurerm_network_security_group" "aks_private" {
  name                = "${var.vnet_name}-aks-private-nsg"
  location            = var.location
  resource_group_name = var.resource_group_name

  security_rule {
    name                       = "Allow-Azure-LB-Health-Probe"
    priority                   = 1000
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "AzureLoadBalancer"
    destination_address_prefix = "*"
    description                = "Allow Azure Load Balancer health probes"
  }

  security_rule {
    name                       = "Allow-Public-Ingress"
    priority                   = 1001
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_ranges    = ["80", "443"]
    source_address_prefix      = "Internet"
    destination_address_prefix = "*"
    description                = "Allow HTTP and HTTPS from the Internet to the Load Balancer"
  }

  security_rule {
    name                       = "Allow-NodePorts-Debug"
    priority                   = 1003
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_ranges    = ["30000-32767"]
    source_address_prefix      = "*"
    destination_address_prefix = "*"
    description                = "Allow direct access to AKS nodePort traffic for debugging"
  }
}



# Associate NSG with all private subnets
resource "azurerm_subnet_network_security_group_association" "private" {
  for_each                  = azurerm_subnet.private
  subnet_id                 = each.value.id
  network_security_group_id = azurerm_network_security_group.aks_private.id
}
