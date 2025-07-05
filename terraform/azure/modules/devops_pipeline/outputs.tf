# Outputs for devops_pipeline module

output "pipeline_id" {
  description = "The Azure DevOps Pipeline resource ID."
  value       = azuredevops_build_definition.this.id
}

output "pipeline_name" {
  description = "The name of the Azure DevOps Pipeline resource."
  value       = azuredevops_build_definition.this.name
}