terraform {
  required_providers {
    azapi = {
      source  = "azure/azapi"
      version = "~> 1.12.0"
    }
  }
}
resource "azapi_resource" "openai" {
  type      = "Microsoft.CognitiveServices/accounts@2023-05-01"
  name      = "${var.prefix}-openai"
  location  = var.location
  parent_id = var.resource_group_id

  body = jsonencode({
    sku = {
      name = "S0"
    }
    kind       = "OpenAI"
    properties = {}
  })
}


resource "azapi_resource" "o4mini_deployment" {
  type      = "Microsoft.CognitiveServices/accounts/deployments@2023-05-01"
  name      = "o4-mini"
  parent_id = azapi_resource.openai.id

  body = jsonencode({
    sku = {
      name     = "GlobalStandard"
      capacity = 50
    }
    properties = {
      model = {
        format  = "OpenAI"
        name    = "o4-mini"
        version = "2025-04-16"
      }
    }
  })
}
