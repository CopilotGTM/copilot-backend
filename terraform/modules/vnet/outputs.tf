output "vnet_name" {
  description = "The name of the created Virtual Network."
  value       = azurerm_virtual_network.this.name
}

output "vnet_id" {
  description = "The ID of the created Virtual Network."
  value       = azurerm_virtual_network.this.id
}

output "address_space" {
  description = "The address space of the created Virtual Network."
  value       = azurerm_virtual_network.this.address_space
}

output "public_subnet_ids" {
  description = "IDs of all public subnets."
  value       = [for s in azurerm_subnet.public : s.id]
}

output "private_subnet_ids" {
  description = "IDs of all private subnets."
  value       = [for s in azurerm_subnet.private : s.id]
}