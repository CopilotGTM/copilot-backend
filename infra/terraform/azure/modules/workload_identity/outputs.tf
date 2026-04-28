output "client_id" {
  description = "Client ID of the AAD application/service principal"
  value       = azuread_application.this.client_id
}

output "object_id" {
  description = "Object ID of the service principal"
  value       = azuread_service_principal.this.object_id
}

output "federated_identity_credential_id" {
  description = "ID of the federated identity credential"
  value       = azuread_application_federated_identity_credential.fic.id
}

output "client_secret" {
  description = "Client secret (only if create_password true)"
  value       = var.create_password ? one(azuread_service_principal_password.this[*].value) : null
  sensitive   = true
}
