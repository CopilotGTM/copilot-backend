terraform {
  required_providers {
    azapi = {
      source  = "Azure/azapi"
      version = ">= 2.5.0"
    }
  }
}

# -----------------------------------------------------------------
# Azure OpenAI Account
# -----------------------------------------------------------------
resource "azapi_resource" "openai" {
  type      = "Microsoft.CognitiveServices/accounts@2024-10-01"
  name      = "${var.prefix}-openai-${var.env}"
  location  = var.location
  parent_id = var.resource_group_id

  body = {
    sku = {
      name = var.sku_name
    }
    kind = "OpenAI"
    properties = {
      customSubDomainName = "${var.prefix}-openai-${var.env}"
      publicNetworkAccess = var.public_network_access
    }
  }

  tags = var.tags

  lifecycle {
    # Prevent updates to the account - avoid azapi "Missing Resource Identity" bug
    ignore_changes = [body, identity]
  }
}

# -----------------------------------------------------------------
# Dynamic Model Deployments
# -----------------------------------------------------------------
# Deployments are created sequentially to avoid Azure API conflicts
resource "azapi_resource" "model_deployment" {
  for_each = var.models

  type                      = "Microsoft.CognitiveServices/accounts/deployments@2024-10-01"
  name                      = each.value.deployment_name
  parent_id                 = azapi_resource.openai.id
  schema_validation_enabled = false

  body = {
    properties = {
      model = {
        format    = "OpenAI"
        name      = each.value.model_name
        version   = lookup(each.value, "version", null)
        publisher = lookup(each.value, "publisher", null)
      }
    }
    sku = {
      name     = each.value.sku
      capacity = each.value.capacity
    }
  }

  # Deploy models sequentially to avoid Azure API rate limits
  depends_on = [azapi_resource.openai]

  lifecycle {
    # Ignore identity to avoid azapi "Missing Resource Identity" bug on update
    ignore_changes = [identity]
  }
}

# -----------------------------------------------------------------
# Retrieve API Keys
# -----------------------------------------------------------------
data "azapi_resource_action" "openai_keys" {
  type                   = "Microsoft.CognitiveServices/accounts@2024-10-01"
  resource_id            = azapi_resource.openai.id
  action                 = "listKeys"
  response_export_values = ["key1", "key2"]
}
