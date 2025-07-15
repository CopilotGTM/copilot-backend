terraform {
  required_providers {
    azapi = {
      source  = "Azure/azapi"
      version = ">= 2.5.0"
    }
  }
}

provider "azapi" {}

resource "azapi_resource" "openai" {
  type      = "Microsoft.CognitiveServices/accounts@2023-05-01"
  name      = "${var.prefix}-openai"
  location  = var.location
  parent_id = var.resource_group_id

  body = {
    sku        = { name = "S0" }
    kind       = "OpenAI"
    properties = {}
  }
}

resource "azapi_resource" "o4mini_deployment" {
  type      = "Microsoft.CognitiveServices/accounts/deployments@2023-05-01"
  name      = "o4-mini"
  parent_id = azapi_resource.openai.id

  body = {
    sku = {
      name     = "GlobalStandard"
      capacity = 100
    }
    properties = {
      model = {
        format  = "OpenAI"
        name    = "o4-mini"
        version = "2025-04-16"
      }
    }
  }
}

resource "azapi_resource" "embedder_deployment" {
  type                      = "Microsoft.CognitiveServices/accounts/deployments@2025-06-01"
  name                      = "text-embedding-3-large"
  parent_id                 = azapi_resource.openai.id
  schema_validation_enabled = false   # until AzAPI ships the 2025-06 schema

  body = {
    properties = {
      model = {
        format    = "OpenAI"
        name      = "text-embedding-3-large"
        publisher = "OpenAI"          # required since 2024-04-01 API
      }
    }
    sku = {
      name     = "Standard"           # or "GlobalStandard" if you need cross-region
      capacity = 200                    # 1 = ~100 TPM / 1 RPM for embeddings
    }
  }
}
