variable "devops_project_id" {
  description = "The Azure DevOps Project ID."
  type        = string
}

variable "service_endpoint_name" {
  description = "The name for the Azure DevOps Docker registry service connection."
  type        = string
}

variable "acr_login_server" {
  description = "The Azure Container Registry login server."
  type        = string
}

variable "acr_sp_app_id" {
  description = "The service principal client ID for ACR authentication."
  type        = string
}

variable "acr_sp_app_secret" {
  description = "The service principal client secret for ACR authentication."
  type        = string
  sensitive   = true
}