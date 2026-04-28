output "storage_account_id" {
  value = azurerm_storage_account.this.id
}

output "storage_account_name" {
  value = azurerm_storage_account.this.name
}

output "primary_connection_string" {
  value     = azurerm_storage_account.this.primary_connection_string
  sensitive = true
}

output "primary_blob_endpoint" {
  value = azurerm_storage_account.this.primary_blob_endpoint
}

output "private_endpoint_id" {
  value = try(azurerm_private_endpoint.this[0].id, null)
}

output "private_endpoint_private_ip" {
  value = try(azurerm_private_endpoint.this[0].private_service_connection[0].private_ip_address, null)
}