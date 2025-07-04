output "mongodb_account_id" {
  value = azurerm_cosmosdb_account.this.id
}

output "mongodb_account_name" {
  value = azurerm_cosmosdb_account.this.name
}

output "endpoint" {
  value = azurerm_cosmosdb_account.this.endpoint
}

output "primary_connection_string" {
  value     = azurerm_cosmosdb_account.this.primary_connection_string
  sensitive = true
}