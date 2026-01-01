# -----------------------------------------------------------------
# Required Variables
# -----------------------------------------------------------------
variable "prefix" {
  description = "A prefix to apply to all resources created by this module."
  type        = string
}

variable "env" {
  description = "Environment name (dev, uat, prod)."
  type        = string
}

variable "resource_group_id" {
  description = "The ID of the Azure resource group to use as parent for the OpenAI account."
  type        = string
}

variable "location" {
  description = "The Azure region where the resources will be deployed."
  type        = string
}

# -----------------------------------------------------------------
# Account Configuration
# -----------------------------------------------------------------
variable "sku_name" {
  description = "The SKU name for the Azure OpenAI account."
  type        = string
  default     = "S0"
}

variable "public_network_access" {
  description = "Whether to allow public network access to the OpenAI account."
  type        = string
  default     = "Enabled"
}

variable "tags" {
  description = "Tags to apply to the Azure OpenAI resources."
  type        = map(string)
  default     = {}
}

# -----------------------------------------------------------------
# Model Deployments
# -----------------------------------------------------------------
variable "models" {
  description = <<-EOT
    Map of model deployments to create. Each model requires:
    - deployment_name: Name for the deployment (used as resource name)
    - model_name: The actual model name (e.g., "gpt-4", "o4-mini", "text-embedding-3-large")
    - sku: SKU type ("Standard", "GlobalStandard", etc.)
    - capacity: Capacity in thousands of tokens per minute
    - version: (Optional) Model version
    - publisher: (Optional) Model publisher (e.g., "OpenAI")

    Example:
    models = {
      chat = {
        deployment_name = "gpt-4"
        model_name      = "gpt-4"
        sku             = "GlobalStandard"
        capacity        = 100
        version         = "turbo-2024-04-09"
      }
      embedder = {
        deployment_name = "text-embedding-3-large"
        model_name      = "text-embedding-3-large"
        sku             = "Standard"
        capacity        = 200
        publisher       = "OpenAI"
      }
    }
  EOT
  type = map(object({
    deployment_name = string
    model_name      = string
    sku             = string
    capacity        = number
    version         = optional(string)
    publisher       = optional(string)
  }))
  default = {}
}
