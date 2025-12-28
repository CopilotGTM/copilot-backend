output "waf_id" {
  description = "The ID of the Application Gateway"
  value       = azurerm_application_gateway.waf.id
}

output "waf_name" {
  description = "The Name of the Application Gateway"
  value       = azurerm_application_gateway.waf.name
}

output "public_ip_address" {
  description = "The Public IP Address of the Application Gateway"
  value       = azurerm_public_ip.waf.ip_address
}

output "public_ip_id" {
  description = "The ID of the Public IP Address"
  value       = azurerm_public_ip.waf.id
}
