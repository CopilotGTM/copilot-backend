output "pipeline_id" {
  description = "The ID of the Azure DevOps pipeline."
  value       = azuredevops_build_definition.pipeline.id
}

output "pipeline_name" {
  description = "The name of the Azure DevOps pipeline."
  value       = azuredevops_build_definition.pipeline.name
}