variable "devops_project_id" {
  description = "The Azure DevOps project ID."
  type        = string
}

variable "service_connection_name" {
  description = "The name for the Azure DevOps service connection."
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
  description = "Service principal client (application) ID."
  type        = string
}

variable "client_secret" {
  description = "Service principal client secret."
  type        = string
  sensitive   = true
}