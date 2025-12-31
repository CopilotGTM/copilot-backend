output "aks_name" {
  description = "The name of the AKS cluster."
  value       = azurerm_kubernetes_cluster.this.name
}

output "aks_id" {
  description = "The Azure resource ID of the AKS cluster."
  value       = azurerm_kubernetes_cluster.this.id
}

output "kube_config" {
  description = "Kube config to connect to the AKS cluster."
  value       = azurerm_kubernetes_cluster.this.kube_config_raw
  sensitive   = true
}

output "host" {
  description = "The Kubernetes cluster API server address."
  value       = azurerm_kubernetes_cluster.this.kube_config[0].host
}

output "client_certificate" {
  description = "The client certificate for authenticating to the cluster."
  value       = azurerm_kubernetes_cluster.this.kube_config[0].client_certificate
  sensitive   = true
}

output "client_key" {
  description = "The client key for authenticating to the cluster."
  value       = azurerm_kubernetes_cluster.this.kube_config[0].client_key
  sensitive   = true
}

output "cluster_ca_certificate" {
  description = "The cluster CA certificate."
  value       = azurerm_kubernetes_cluster.this.kube_config[0].cluster_ca_certificate
  sensitive   = true
}

output "aks_principal_id" {
  description = "The managed identity principal ID of the AKS cluster."
  value       = azurerm_kubernetes_cluster.this.identity[0].principal_id
}

output "kubelet_identity_object_id" {
  description = "The object ID of the kubelet identity."
  value       = azurerm_kubernetes_cluster.this.kubelet_identity[0].object_id
}

output "oidc_issuer_url" {
  description = "The OIDC issuer URL for the AKS cluster."
  value       = azurerm_kubernetes_cluster.this.oidc_issuer_url
}

output "workload_identity_enabled" {
  description = "Whether workload identity is enabled for the AKS cluster."
  value       = azurerm_kubernetes_cluster.this.workload_identity_enabled
}

# GPU Node Pool Outputs
output "gpu_node_pool_enabled" {
  description = "Whether the GPU node pool is enabled and configured."
  value       = var.enable_gpu_worker_pool
}

output "gpu_node_pool_name" {
  description = "Name of the GPU node pool for knowledge workers."
  value       = var.enable_gpu_worker_pool ? azurerm_kubernetes_cluster_node_pool.gpu[0].name : null
}

output "gpu_node_pool_id" {
  description = "Azure resource ID of the GPU node pool."
  value       = var.enable_gpu_worker_pool ? azurerm_kubernetes_cluster_node_pool.gpu[0].id : null
}

output "gpu_vm_size" {
  description = "VM size/SKU configured for GPU nodes."
  value       = var.enable_gpu_worker_pool ? var.gpu_vm_size : null
}

output "gpu_scaling_config" {
  description = "Autoscaling configuration for the GPU node pool."
  value = var.enable_gpu_worker_pool ? {
    min_count     = var.gpu_min_count
    max_count     = var.gpu_max_count
    scale_to_zero = var.gpu_min_count == 0
    estimated_cost = {
      idle_per_hour   = var.gpu_min_count == 0 ? "$0.00" : "$${var.gpu_min_count * 0.526}"
      active_per_hour = "$${var.gpu_max_count * 0.526}"
      monthly_max     = "$${var.gpu_max_count * 0.526 * 24 * 30}"
    }
  } : null
}

output "gpu_node_labels" {
  description = "Kubernetes labels applied to GPU nodes."
  value       = var.enable_gpu_worker_pool ? var.gpu_node_labels : null
}

output "gpu_node_taints" {
  description = "Kubernetes taints applied to GPU nodes for workload isolation."
  value       = var.enable_gpu_worker_pool ? var.gpu_node_taints : null
}
