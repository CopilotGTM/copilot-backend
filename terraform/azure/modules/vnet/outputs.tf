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

output "public_subnet_names" {
  description = "Names of all public subnets."
  value       = [for s in azurerm_subnet.public : s.name]
}

output "private_subnet_names" {
  description = "Names of all private subnets."
  value       = [for s in azurerm_subnet.private : s.name]
}

output "public_subnet_address_prefixes" {
  description = "Address prefixes of all public subnets."
  value       = flatten([for s in azurerm_subnet.public : s.address_prefixes])
}

output "private_subnet_address_prefixes" {
  description = "Address prefixes of all private subnets."
  value       = flatten([for s in azurerm_subnet.private : s.address_prefixes])
}

output "nat_gateway_public_ip" {
  description = "The public IP address of the NAT Gateway."
  value       = azurerm_public_ip.nat_gw.ip_address
}

output "private_endpoints_subnet_id" {
  description = "ID of the dedicated private endpoints subnet (if created)."
  value       = try(azurerm_subnet.private_endpoints[0].id, null)
}

output "private_endpoints_subnet_name" {
  description = "Name of the dedicated private endpoints subnet (if created)."
  value       = try(azurerm_subnet.private_endpoints[0].name, null)
}