variable "devops_project_id" {
  description = "The Azure DevOps Project ID."
  type        = string
}

variable "service_endpoint_name" {
  description = "The name for the Azure DevOps ACR service connection."
  type        = string
}

variable "resource_group_name" {
  description = "The resource group containing the ACR."
  type        = string
}

variable "acr_name" {
  description = "The Azure Container Registry name."
  type        = string
}

variable "subscription_id" {
  description = "The Azure subscription ID."
  type        = string
}

variable "subscription_name" {
  description = "The Azure subscription name."
  type        = string
}

variable "tenant_id" {
  description = "The Azure AD tenant ID."
  type        = string
}

variable "client_id" {
  description = "Service principal client (application) ID for OIDC authentication."
  type        = string
}
