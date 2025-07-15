output "openai_account_id" {
  description = "The ID of the Azure OpenAI account."
  value       = azapi_resource.openai.id
}

output "openai_account_name" {
  description = "The name of the OpenAI account resource."
  value       = azapi_resource.openai.name
}

output "openai_endpoint_url" {
  description = "The endpoint URL for Azure OpenAI completion API calls. Constructed value."
  value       = "https://${azapi_resource.openai.name}.openai.azure.com/"
}

output "embedder_deployment_name" {
  description = "The name of the embedder deployment."
  value       = azapi_resource.embedder_deployment.name
}
