output "db_server_id" {
  description = "The ID of the PostgreSQL Flexible Server."
  value       = azurerm_postgresql_flexible_server.db.id
}

output "db_server_name" {
  description = "The name of the PostgreSQL Flexible Server."
  value       = azurerm_postgresql_flexible_server.db.name
}

output "db_password_secret_id" {
  description = "The ID of the Key Vault secret containing the database password."
  value       = azurerm_key_vault_secret.db_password.id
}