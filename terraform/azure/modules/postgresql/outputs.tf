output "db_server_id" {
  description = "The ID of the PostgreSQL Flexible Server."
  value       = azurerm_postgresql_flexible_server.db.id
}

output "db_server_name" {
  description = "The name of the PostgreSQL Flexible Server."
  value       = azurerm_postgresql_flexible_server.db.name
}

output "db_host" {
  description = "The FQDN/host of the PostgreSQL Flexible Server."
  value       = azurerm_postgresql_flexible_server.db.fqdn
}

output "db_port" {
  description = "The port of the PostgreSQL Flexible Server."
  value       = 5432
}

output "db_user" {
  description = "The admin user of the PostgreSQL Flexible Server."
  value       = var.db_admin_user
}

output "db_name" {
  description = "The default database name to connect to."
  value       = "postgres"
}

output "db_password_secret_id" {
  description = "The ID of the Key Vault secret containing the database password."
  value       = azurerm_key_vault_secret.db_password.id
}