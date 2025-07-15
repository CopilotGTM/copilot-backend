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

output "node_pool_backend_pool_id" {
  description = "The backend pool ID for the default node pool."
  value       = azurerm_kubernetes_cluster.this.default_node_pool[0].backend_address_pool_id
}
