output "openai_account_id" {
  description = "The ID of the Azure OpenAI account."
  value       = azapi_resource.openai.id
}
