data "azuread_client_config" "current" {}

data "azurerm_key_vault_secret" "supabase_url" {
  name         = var.supabase_url_secret_name
  key_vault_id = var.key_vault_id
}

locals {
  microsoft_graph_app_id = "00000003-0000-0000-c000-000000000000"
  display_name           = coalesce(var.display_name_override, "CopilotGTM Login - ${title(var.env)}")
  owner_object_id        = coalesce(var.owner_object_id, data.azuread_client_config.current.object_id)
  supabase_callback_url  = "${trimsuffix(data.azurerm_key_vault_secret.supabase_url.value, "/")}/auth/v1/callback"
}

resource "azuread_application" "login" {
  display_name     = local.display_name
  sign_in_audience = "AzureADMultipleOrgs"
  owners           = [local.owner_object_id]

  web {
    redirect_uris = [local.supabase_callback_url]

    implicit_grant {
      access_token_issuance_enabled = false
      id_token_issuance_enabled     = false
    }
  }

  # These OIDC scopes do not intrinsically require admin consent. Tenant consent
  # policy may still require an admin to approve sign-in for that tenant.
  required_resource_access {
    resource_app_id = local.microsoft_graph_app_id

    resource_access {
      id   = "37f7f235-527c-4136-accd-4a02d197296e" # openid
      type = "Scope"
    }

    resource_access {
      id   = "14dad69e-099b-42c9-810b-d002981feec1" # profile
      type = "Scope"
    }

    resource_access {
      id   = "64a6cdd6-aab1-4aaf-94b8-3cc8405e90d0" # email
      type = "Scope"
    }
  }

  tags = ["CopilotGTM", "Login", "OAuth", var.env]

  lifecycle {
    precondition {
      condition     = can(regex("^https://[^/]+/?$", data.azurerm_key_vault_secret.supabase_url.value))
      error_message = "The Supabase URL Key Vault secret must contain an HTTPS origin without a path."
    }
  }
}

resource "azuread_service_principal" "login" {
  client_id                    = azuread_application.login.client_id
  app_role_assignment_required = false
  owners                       = [local.owner_object_id]
}

resource "azuread_application_password" "login" {
  application_id = azuread_application.login.id
  display_name   = "CopilotGTM Login Secret - ${var.env}"
  end_date       = timeadd(timestamp(), "17520h")

  lifecycle {
    ignore_changes = [end_date]
  }
}

resource "azurerm_key_vault_secret" "client_id" {
  name         = "microsoft-login-client-id"
  value        = azuread_application.login.client_id
  key_vault_id = var.key_vault_id
  tags         = var.tags
}

resource "azurerm_key_vault_secret" "client_secret" {
  name         = "microsoft-login-client-secret"
  value        = azuread_application_password.login.value
  key_vault_id = var.key_vault_id
  tags         = var.tags
}
