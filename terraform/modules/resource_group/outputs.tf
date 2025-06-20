output "resource_group_name" {
  description = "The name of the created Azure Resource Group."
  value       = azurerm_resource_group.this.name
}

output "resource_group_location" {
  description = "The location of the created Azure Resource Group."
  value       = azurerm_resource_group.this.location
}

output "resource_group_id" {
  description = "The ID of the created Azure Resource Group."
  value       = azurerm_resource_group.this.id
}