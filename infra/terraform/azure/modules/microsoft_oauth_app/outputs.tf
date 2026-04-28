output "application_id" {
  description = "Azure AD Application (object) ID"
  value       = azuread_application.copilotgtm.id
}

output "client_id" {
  description = "Application (client) ID used for OAuth flows"
  value       = azuread_application.copilotgtm.client_id
}

output "client_secret" {
  description = "Client secret value (sensitive)"
  value       = azuread_application_password.copilotgtm.value
  sensitive   = true
}

output "service_principal_id" {
  description = "Service principal object ID (for admin consent)"
  value       = azuread_service_principal.copilotgtm.object_id
}
