resource "azurerm_kubernetes_cluster" "this" {
  name                = var.aks_name
  location            = var.location
  resource_group_name = var.resource_group_name
  dns_prefix          = var.dns_prefix

  default_node_pool {
    name            = "agentpool"
    node_count      = var.agent_count
    vm_size         = var.vm_size
    vnet_subnet_id  = var.vnet_subnet_ids[0]
    os_disk_size_gb = var.os_disk_size_gb
    temporary_name_for_rotation = "tempnp"
  }

  network_profile {
    network_plugin     = "azure"
    service_cidr       = var.service_cidr
    dns_service_ip     = var.dns_service_ip
    load_balancer_sku  = "standard"
    outbound_type      = "loadBalancer"
  }

  identity {
    type = "SystemAssigned"
  }
  
  oidc_issuer_enabled = var.oidc_issuer_enabled
  workload_identity_enabled = var.workload_identity_enabled

  key_vault_secrets_provider {
    secret_rotation_enabled  = true
    secret_rotation_interval = "2m"
  }

  tags = {
    env = var.env
  }
}
