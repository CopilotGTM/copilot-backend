# -----------------------------------------------------------------
# Account Outputs
# -----------------------------------------------------------------
output "openai_account_id" {
  description = "The ID of the Azure OpenAI account."
  value       = azapi_resource.openai.id
}

output "openai_account_name" {
  description = "The name of the OpenAI account resource."
  value       = azapi_resource.openai.name
}

output "openai_endpoint" {
  description = "The endpoint URL for Azure OpenAI API calls."
  value       = "https://${azapi_resource.openai.name}.openai.azure.com/"
}

# -----------------------------------------------------------------
# API Keys (Retrieved from Azure)
# -----------------------------------------------------------------
output "primary_key" {
  description = "The primary API key for the Azure OpenAI account."
  value       = data.azapi_resource_action.openai_keys.output.key1
  sensitive   = true
}

output "secondary_key" {
  description = "The secondary API key for the Azure OpenAI account."
  value       = data.azapi_resource_action.openai_keys.output.key2
  sensitive   = true
}

# -----------------------------------------------------------------
# Model Deployment Outputs
# -----------------------------------------------------------------
output "deployments" {
  description = "Map of all deployed models with their names and IDs."
  value = {
    for key, deployment in azapi_resource.model_deployment : key => {
      id              = deployment.id
      name            = deployment.name
      deployment_name = var.models[key].deployment_name
      model_name      = var.models[key].model_name
    }
  }
}

output "deployment_names" {
  description = "Map of deployment keys to their deployment names."
  value = {
    for key, deployment in azapi_resource.model_deployment : key => deployment.name
  }
}
