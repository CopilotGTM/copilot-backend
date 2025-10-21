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
    
    upgrade_settings {
      drain_timeout_in_minutes      = 0
      max_surge                     = "10%"
      node_soak_duration_in_minutes = 0
    }
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

# -----------------------------------------------------------------------------
# Optional GPU (knowledge worker) node pool (separate to allow scale-to-zero)
# Uses autoscaling and taints so only pods with matching tolerations schedule.
# Requires azurerm provider v4.0+ for enable_auto_scaling support
# -----------------------------------------------------------------------------
resource "azurerm_kubernetes_cluster_node_pool" "gpu" {
  count                 = var.enable_gpu_worker_pool ? 1 : 0
  name                  = var.gpu_node_pool_name
  kubernetes_cluster_id = azurerm_kubernetes_cluster.this.id
  vm_size               = var.gpu_vm_size
  
  # Autoscaling configuration for scale-to-zero
  auto_scaling_enabled  = true
  min_count             = var.gpu_min_count  # 0 for scale-to-zero
  max_count             = var.gpu_max_count  # 1 for single GPU worker
  
  os_disk_size_gb       = var.os_disk_size_gb
  vnet_subnet_id        = var.vnet_subnet_ids[0]
  orchestrator_version  = azurerm_kubernetes_cluster.this.kubernetes_version

  node_labels           = var.gpu_node_labels
  node_taints           = var.gpu_node_taints

  tags = {
    env       = var.env
    dedicated = "knowledge-gpu"
  }
}
