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

# -----------------------------------------------------------------
# VNet Outputs
# -----------------------------------------------------------------
output "vnet_name" {
  description = "The name of the created Virtual Network."
  value       = module.vnet.vnet_name
}

output "vnet_id" {
  description = "The ID of the created Virtual Network."
  value       = module.vnet.vnet_id
}

output "vnet_address_space" {
  description = "The address space of the created Virtual Network."
  value       = module.vnet.address_space
}

output "public_subnet_ids" {
  description = "IDs of all public subnets."
  value       = module.vnet.public_subnet_ids
}

output "private_subnet_ids" {
  description = "IDs of all private subnets."
  value       = module.vnet.private_subnet_ids
}

output "public_subnet_names" {
  description = "Names of all public subnets."
  value       = module.vnet.public_subnet_names
}

output "private_subnet_names" {
  description = "Names of all private subnets."
  value       = module.vnet.private_subnet_names
}

output "public_subnet_address_prefixes" {
  description = "Address prefixes of all public subnets."
  value       = module.vnet.public_subnet_address_prefixes
}

output "private_subnet_address_prefixes" {
  description = "Address prefixes of all private subnets."
  value       = module.vnet.private_subnet_address_prefixes
}

output "nat_gateway_public_ip" {
  description = "The public IP address of the NAT Gateway."
  value       = module.vnet.nat_gateway_public_ip
}

# # ---- ACR outputs for pipeline use ----
# output "acr_login_server" {
#   description = "The login server URL of the Azure Container Registry."
#   value       = module.acr.acr_login_server
# }
# output "acr_id" {
#   description = "The ID of the Azure Container Registry."
#   value       = module.acr.acr_id
# }
# # Admin credentials not surfaced (admin_enabled = false)

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