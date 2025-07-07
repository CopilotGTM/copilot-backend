variable "account_name" {
  description = "Name of the Azure Cognitive (OpenAI) account."
  type        = string
}

variable "location" {
  description = "Azure region to deploy to."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group in which to create the OpenAI account."
  type        = string
}

variable "sku_name" {
  description = "SKU name for the Cognitive Services account."
  type        = string
  default     = "S0"
}

variable "public_network_access_enabled" {
  description = "Enable public network access to the resource."
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags for the resource."
  type        = map(string)
  default     = {}
}

variable "deployment_name" {
  description = "Name of the model deployment."
  type        = string
}

variable "model_format" {
  description = "Format of the model ('OpenAI')."
  type        = string
  default     = "OpenAI"
}

variable "model_name" {
  description = "Name of the deployed model (e.g., 'gpt-35-turbo')."
  type        = string
}

variable "model_version" {
  description = "Version of the model to deploy (e.g., '0613')."
  type        = string
}

variable "scale_type" {
  description = "Type of scaling (e.g., 'Standard')."
  type        = string
  default     = "Standard"
}

variable "scale_capacity" {
  description = "Capacity for deployment scaling."
  type        = number
  default     = 1
}

variable "deployment_sku_name" {
  description = "SKU name for deployment (e.g., 'STANDARD')."
  type        = string
  default     = "STANDARD"
}

variable "deployment_sku_capacity" {
  description = "Deployment instance count/capacity."
  type        = number
  default     = 1
}