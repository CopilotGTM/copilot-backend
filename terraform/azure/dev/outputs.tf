output "resource_group_name" {
  description = "The name of the created Azure Resource Group."
  value       = module.resource_group.resource_group_name
}

output "resource_group_location" {
  description = "The location of the created Azure Resource Group."
  value       = module.resource_group.resource_group_location
}

output "resource_group_id" {
  description = "The ID of the created Azure Resource Group."
  value       = module.resource_group.resource_group_id
}

# ---- ACR outputs for pipeline use ----
output "acr_login_server" {
  description = "The login server URL of the Azure Container Registry."
  value       = module.acr.acr_login_server
}
output "acr_id" {
  description = "The ID of the Azure Container Registry."
  value       = module.acr.acr_id
}
# Admin credentials not surfaced (admin_enabled = false)

# ---- AKS outputs for pipeline use ----
output "aks_name" {
  description = "The name of the AKS cluster."
  value       = module.aks.aks_name
}
output "aks_id" {
  description = "The Azure resource ID of the AKS cluster."
  value       = module.aks.aks_id
}
output "aks_kube_config" {
  description = "Kube config to connect to the AKS cluster."
  value       = module.aks.kube_config
  sensitive   = true
}
output "aks_principal_id" {
  description = "The managed identity principal ID of the AKS cluster."
  value       = module.aks.aks_principal_id
}