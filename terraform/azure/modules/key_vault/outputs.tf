output "key_vault_id" {
  description = "The ID of the Key Vault."
  value       = azurerm_key_vault.this.id
}

output "key_vault_uri" {
  description = "The URI of the Key Vault."
  value       = azurerm_key_vault.this.vault_uri
}

output "private_endpoint_id" {
  description = "The ID of the Key Vault private endpoint (if enabled)."
  value       = try(azurerm_private_endpoint.this[0].id, null)
}

output "private_endpoint_private_ip" {
  description = "The private IP of the Key Vault private endpoint (if enabled)."
  value       = try(azurerm_private_endpoint.this[0].private_service_connection[0].private_ip_address, null)
}