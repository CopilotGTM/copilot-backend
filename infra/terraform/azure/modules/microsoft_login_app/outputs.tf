output "application_id" {
  description = "Entra application object ID"
  value       = azuread_application.login.id
}

output "client_id" {
  description = "Application client ID configured in the Supabase Azure provider"
  value       = azuread_application.login.client_id
}

output "service_principal_id" {
  description = "Enterprise application service principal object ID"
  value       = azuread_service_principal.login.object_id
}

output "supabase_callback_url" {
  description = "Supabase Auth callback registered in Entra"
  value       = local.supabase_callback_url
  sensitive   = true
}
