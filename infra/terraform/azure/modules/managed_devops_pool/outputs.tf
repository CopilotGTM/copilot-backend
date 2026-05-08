output "id" {
  description = "Managed DevOps Pool resource ID."
  value       = azapi_resource.pool.id
}

output "name" {
  description = "Managed DevOps Pool name."
  value       = azapi_resource.pool.name
}

output "agent_state" {
  description = "Configured agent lifecycle state."
  value       = var.agent_state
}
