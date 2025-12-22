locals {
  effective_public_network_access_enabled = var.private_endpoint_enabled ? false : var.public_network_access_enabled
  private_endpoint_name                   = "${var.storage_account_name}-pe"
}

resource "azurerm_storage_account" "this" {
  name                     = var.storage_account_name
  resource_group_name      = var.resource_group_name
  location                 = var.location
  account_tier             = var.account_tier
  account_replication_type = var.account_replication_type

  public_network_access_enabled = local.effective_public_network_access_enabled

  dynamic "blob_properties" {
    for_each = length(var.allowed_blob_cors_origins) > 0 ? [1] : []
    content {
      cors_rule {
        allowed_headers    = ["*"]
        allowed_methods    = ["GET","HEAD","POST","PUT","DELETE","OPTIONS"]
        allowed_origins    = var.allowed_blob_cors_origins
        exposed_headers    = ["*"]
        max_age_in_seconds = 3600
      }
    }
  }

  tags = var.tags
}

resource "azurerm_private_endpoint" "this" {
  count               = var.private_endpoint_enabled ? 1 : 0
  name                = local.private_endpoint_name
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.private_endpoint_subnet_id

  private_service_connection {
    name                           = "${local.private_endpoint_name}-psc"
    is_manual_connection           = false
    private_connection_resource_id = azurerm_storage_account.this.id
    subresource_names              = var.private_endpoint_subresource_names
  }

  dynamic "private_dns_zone_group" {
    for_each = var.private_dns_zone_ids != null ? [1] : []
    content {
      name                 = "default"
      private_dns_zone_ids = var.private_dns_zone_ids
    }
  }
}
