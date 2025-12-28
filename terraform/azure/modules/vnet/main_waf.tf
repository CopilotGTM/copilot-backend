resource "azurerm_subnet" "waf" {
  count                = var.waf_subnet_prefix != "" ? 1 : 0
  name                 = "${var.vnet_name}-waf-subnet"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [var.waf_subnet_prefix]
}
