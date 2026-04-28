output "resource_group_name" {
  description = "Resource group hosting Nango infrastructure"
  value       = module.resource_group.resource_group_name
}

output "resource_group_location" {
  description = "Azure region of the resource group"
  value       = module.resource_group.resource_group_location
}

output "vnet_name" {
  description = "Virtual network name"
  value       = module.vnet.vnet_name
}

output "vnet_id" {
  description = "Virtual network resource ID"
  value       = module.vnet.vnet_id
}

output "public_subnet_ids" {
  description = "IDs of public subnets"
  value       = module.vnet.public_subnet_ids
}

output "private_subnet_ids" {
  description = "IDs of private subnets"
  value       = module.vnet.private_subnet_ids
}
