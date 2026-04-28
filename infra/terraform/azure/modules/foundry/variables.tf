variable "prefix" {
  description = "A prefix to apply to all resources created by this module."
  type        = string
}

variable "resource_group_name" {
  description = "The name of the resource group to deploy the resources into."
  type        = string
}

variable "location" {
  description = "The Azure region where the resources will be deployed."
  type        = string
}

variable "resource_group_id" {
  description = "The ID of the Azure resource group to use as parent for the OpenAI account."
  type        = string
}

variable "use_openai" {
  description = "A flag to determine whether to deploy the Azure OpenAI service."
  type        = bool
  default     = false
}
