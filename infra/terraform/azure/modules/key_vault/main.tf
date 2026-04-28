locals {
  effective_public_network_access_enabled = var.private_endpoint_enabled ? false : var.public_network_access_enabled
  private_endpoint_name                   = var.private_endpoint_name != "" ? var.private_endpoint_name : "${var.key_vault_name}-pe"
}

# -----------------------------------------------------------------------------
# Validation Checks (cross-variable validation - runs during plan)
# These ensure required dependencies are provided when private endpoints are enabled
# -----------------------------------------------------------------------------
check "private_endpoint_subnet_required" {
  assert {
    condition     = !var.private_endpoint_enabled || var.private_endpoint_subnet_id != null
    error_message = "private_endpoint_subnet_id is required when private_endpoint_enabled=true."
  }
}

check "private_dns_zone_required" {
  assert {
    condition     = !var.private_endpoint_enabled || var.private_dns_zone_id != null
    error_message = "private_dns_zone_id is required when private_endpoint_enabled=true for proper DNS resolution."
  }
}

check "soc2_public_access_warning" {
  assert {
    condition     = !var.public_network_access_enabled || !var.private_endpoint_enabled
    error_message = "Warning: public_network_access_enabled=true with private_endpoint_enabled=true is unusual. For SOC2 compliance, disable public access when using private endpoints."
  }
}

resource "azurerm_key_vault" "this" {
  name                       = var.key_vault_name
  location                   = var.location
  resource_group_name        = var.resource_group_name
  tenant_id                  = var.tenant_id
  sku_name                   = "standard"
  purge_protection_enabled   = true
  soft_delete_retention_days = 7

  rbac_authorization_enabled    = var.enable_rbac_authorization
  public_network_access_enabled = local.effective_public_network_access_enabled
  tags                          = var.tags
}

resource "azurerm_role_assignment" "admin" {
  scope                = azurerm_key_vault.this.id
  role_definition_name = "Key Vault Administrator"
  principal_id         = var.admin_object_id
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
    private_connection_resource_id = azurerm_key_vault.this.id
    subresource_names              = ["vault"]
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
      error_message = "private_dns_zone_id is required when private_endpoint_enabled=true for proper DNS resolution."
    }
  }
}
