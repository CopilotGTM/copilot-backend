locals {
  effective_public_network_access_enabled = var.private_endpoint_enabled ? false : var.public_network_access_enabled
  private_endpoint_name                   = "${var.redis_name}-pe"
}

check "private_endpoint_subnet_required" {
  assert {
    condition     = !var.private_endpoint_enabled || var.private_endpoint_subnet_id != null
    error_message = "private_endpoint_subnet_id is required when private_endpoint_enabled=true."
  }
}

check "private_dns_zone_required" {
  assert {
    condition     = !var.private_endpoint_enabled || var.private_dns_zone_id != null
    error_message = "private_dns_zone_id is required when private_endpoint_enabled=true."
  }
}

resource "azurerm_redis_cache" "this" {
  name                          = var.redis_name
  location                      = var.location
  resource_group_name           = var.resource_group_name
  capacity                      = var.capacity
  family                        = var.family
  sku_name                      = var.sku_name
  public_network_access_enabled = local.effective_public_network_access_enabled

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
    private_connection_resource_id = azurerm_redis_cache.this.id
    subresource_names              = ["redisCache"]
  }

  dynamic "private_dns_zone_group" {
    for_each = var.private_dns_zone_id != null ? [1] : []
    content {
      name                 = "default"
      private_dns_zone_ids = [var.private_dns_zone_id]
    }
  }

  lifecycle {
    precondition {
      condition     = var.private_endpoint_subnet_id != null
      error_message = "private_endpoint_subnet_id is required when private_endpoint_enabled=true."
    }
    precondition {
      condition     = var.private_dns_zone_id != null
      error_message = "private_dns_zone_id is required when private_endpoint_enabled=true."
    }
  }
}
