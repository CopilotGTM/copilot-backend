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