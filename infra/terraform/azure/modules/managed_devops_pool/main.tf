terraform {
  required_providers {
    azapi = {
      source  = "Azure/azapi"
      version = ">= 2.5.0"
    }
  }
}

locals {
  agent_profile = merge(
    {
      kind = var.agent_state
    },
    var.agent_state == "Stateful" ? {
      gracePeriodTimeSpan = var.stateful_grace_period
      maxAgentLifetime    = var.stateful_max_lifetime
    } : {},
    var.prediction_preference != "" ? {
      resourcePredictionsProfile = {
        kind                 = "Automatic"
        predictionPreference = var.prediction_preference
      }
    } : {}
  )

  storage_profile = merge(
    {
      osDiskStorageAccountType = var.os_disk_storage_account_type
    },
    var.data_disk_size_gib > 0 ? {
      dataDisks = [
        {
          diskSizeGiB        = var.data_disk_size_gib
          caching            = "ReadWrite"
          storageAccountType = var.data_disk_storage_account_type
        }
      ]
    } : {}
  )
}

resource "azapi_resource" "pool" {
  type      = "Microsoft.DevOpsInfrastructure/pools@2025-09-20"
  name      = var.pool_name
  location  = var.location
  parent_id = var.resource_group_id

  identity {
    type         = "UserAssigned"
    identity_ids = [azapi_resource.identity.id]
  }

  body = {
    properties = {
      devCenterProjectResourceId = var.dev_center_project_resource_id
      maximumConcurrency         = var.maximum_concurrency

      organizationProfile = {
        kind = "AzureDevOps"
        organizations = [
          {
            url         = var.ado_org_url
            projects    = var.ado_project_names
            parallelism = var.parallelism
          }
        ]
      }

      agentProfile = local.agent_profile

      fabricProfile = {
        kind = "Vmss"
        sku = {
          name = var.sku_name
        }
        images = [
          {
            wellKnownImageName = var.image_name
            aliases            = [var.image_alias, "${var.image_alias}/latest"]
            buffer             = var.image_buffer
          }
        ]
        networkProfile = {
          subnetId = var.subnet_id
        }
        storageProfile = local.storage_profile
      }

      runtimeConfiguration = {
        workFolder = var.work_folder
      }
    }
  }

  tags = var.tags
}

resource "azapi_resource" "identity" {
  type      = "Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31"
  name      = "${var.pool_name}-identity"
  location  = var.location
  parent_id = var.resource_group_id

  tags = var.tags
}
