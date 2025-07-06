resource "azurerm_kubernetes_cluster" "this" {
  name                = var.aks_name
  location            = var.location
  resource_group_name = var.resource_group_name
  dns_prefix          = var.dns_prefix

  default_node_pool {
    name       = "agentpool"
    node_count = var.agent_count
    vm_size    = var.vm_size
    vnet_subnet_id = var.subnet_id
    os_disk_size_gb = var.os_disk_size_gb
    temporary_name_for_rotation = "tempnp"
  }

  network_profile {
    network_plugin     = "azure"
    service_cidr       = var.service_cidr
    dns_service_ip     = var.dns_service_ip
  }

  identity {
    type = "SystemAssigned"
  }

  tags = {
    env = var.env
  }
}