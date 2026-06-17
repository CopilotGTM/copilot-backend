resource "azurerm_key_vault_secret" "db_password" {
  name         = "db-admin-password"
  value        = var.db_admin_password
  key_vault_id = var.key_vault_id
}

check "private_endpoint_subnet_required" {
  assert {
    condition     = !var.private_endpoint_enabled || var.private_endpoint_subnet_id != null
    error_message = "private_endpoint_subnet_id is required when private_endpoint_enabled=true."
  }
}

check "private_dns_zone_required" {
  assert {
    condition     = !var.private_endpoint_enabled || var.private_dns_zone_ids != null
    error_message = "private_dns_zone_ids is required when private_endpoint_enabled=true."
  }
}

resource "azurerm_postgresql_flexible_server" "db" {
  name                          = var.db_name != "" ? "${var.db_name}-${var.env}" : "copilotgtm-postgresdb-${var.env}"
  resource_group_name           = var.resource_group_name
  location                      = var.location
  version                       = var.db_version
  sku_name                      = var.db_sku_name
  storage_mb                    = 65536
  backup_retention_days         = 7
  public_network_access_enabled = var.public_network_access_enabled

  administrator_login    = var.db_admin_user
  administrator_password = azurerm_key_vault_secret.db_password.value

  tags = var.tags

  lifecycle {
    # Azure assigns a zone at creation time. Ignore changes to prevent
    # "zone can only be changed when exchanged with standby_availability_zone" errors.
    ignore_changes = [zone]
  }
}

resource "azurerm_postgresql_flexible_server_configuration" "vector_extension" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.db.id
  value     = "VECTOR"
}

# -----------------------------------------------------------------
# PgBouncer Configuration (Built-in Connection Pooler)
# -----------------------------------------------------------------
resource "azurerm_postgresql_flexible_server_configuration" "pgbouncer_enabled" {
  count     = var.pgbouncer_enabled ? 1 : 0
  name      = "pgbouncer.enabled"
  server_id = azurerm_postgresql_flexible_server.db.id
  value     = "True"
}

resource "azurerm_postgresql_flexible_server_configuration" "pgbouncer_default_pool_size" {
  count     = var.pgbouncer_enabled ? 1 : 0
  name      = "pgbouncer.default_pool_size"
  server_id = azurerm_postgresql_flexible_server.db.id
  value     = tostring(var.pgbouncer_default_pool_size)

  depends_on = [azurerm_postgresql_flexible_server_configuration.pgbouncer_enabled]
}

resource "azurerm_postgresql_flexible_server_configuration" "pgbouncer_max_client_conn" {
  count     = var.pgbouncer_enabled ? 1 : 0
  name      = "pgbouncer.max_client_conn"
  server_id = azurerm_postgresql_flexible_server.db.id
  value     = tostring(var.pgbouncer_max_client_conn)

  depends_on = [azurerm_postgresql_flexible_server_configuration.pgbouncer_enabled]
}

resource "azurerm_postgresql_flexible_server_configuration" "pgbouncer_pool_mode" {
  count     = var.pgbouncer_enabled ? 1 : 0
  name      = "pgbouncer.pool_mode"
  server_id = azurerm_postgresql_flexible_server.db.id
  value     = var.pgbouncer_pool_mode

  depends_on = [azurerm_postgresql_flexible_server_configuration.pgbouncer_enabled]
}

resource "azurerm_postgresql_flexible_server_firewall_rule" "allow_all" {
  count            = var.public_network_access_enabled ? 1 : 0
  name             = "allow-all-ips"
  server_id        = azurerm_postgresql_flexible_server.db.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "255.255.255.255"
}

# resource "azurerm_postgresql_flexible_server_firewall_rule" "allow_pipeline" {
#   count = var.ci_cd_agent_ip_start != "" ? 1 : 0

#   name                = "ci-cd-agent"
#   server_id           = azurerm_postgresql_flexible_server.db.id
#   start_ip_address    = var.ci_cd_agent_ip_start
#   end_ip_address      = var.ci_cd_agent_ip_end
# }

resource "azurerm_private_endpoint" "db" {
  count               = var.private_endpoint_enabled ? 1 : 0
  name                = "pe-postgres-${var.env}"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.private_endpoint_subnet_id

  private_service_connection {
    name                           = "psc-postgres-${var.env}"
    is_manual_connection           = false
    private_connection_resource_id = azurerm_postgresql_flexible_server.db.id
    subresource_names              = ["postgresqlServer"]
  }

  dynamic "private_dns_zone_group" {
    for_each = var.private_dns_zone_ids != null ? [1] : []

    content {
      name                 = "postgresql-private-dns"
      private_dns_zone_ids = var.private_dns_zone_ids
    }
  }
}
