resource "azurerm_key_vault_secret" "db_password" {
  name         = "db-admin-password"
  value        = var.db_admin_password
  key_vault_id = var.key_vault_id
}

resource "azurerm_postgresql_flexible_server" "db" {
  name                   = "copilotgtm-postgresdb-${var.env}"
  resource_group_name    = var.resource_group_name
  location               = var.location
  version                = var.db_version
  sku_name               = "B_Standard_B1ms"
  storage_mb             = 65536
  backup_retention_days  = 7
  public_network_access_enabled = true

  administrator_login    = var.db_admin_user
  administrator_password = azurerm_key_vault_secret.db_password.value

  tags = var.tags
}

# resource "azurerm_postgresql_flexible_server_firewall_rule" "allow_pipeline" {
#   count = var.ci_cd_agent_ip_start != "" ? 1 : 0

#   name                = "ci-cd-agent"
#   server_id           = azurerm_postgresql_flexible_server.db.id
#   start_ip_address    = var.ci_cd_agent_ip_start
#   end_ip_address      = var.ci_cd_agent_ip_end
# }

# resource "azurerm_private_endpoint" "db_pe" {
#   count = var.private_endpoint_subnet_id != "" ? 1 : 0

#   name                = "pe-db-${var.env}"
#   location            = var.location
#   resource_group_name = var.resource_group_name
#   subnet_id           = var.private_endpoint_subnet_id

#   private_service_connection {
#     name                           = "psc-db-${var.env}"
#     is_manual_connection           = false
#     private_connection_resource_id = azurerm_postgresql_flexible_server.db.id
#     subresource_names              = ["postgresqlServer"]
#   }
# }