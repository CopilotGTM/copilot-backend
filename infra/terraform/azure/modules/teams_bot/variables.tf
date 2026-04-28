variable "env" {
  description = "Environment name (dev, uat, prod)"
  type        = string
}

variable "bot_display_name" {
  description = "Display name for the bot shown in Teams"
  type        = string
  default     = "CopilotGTM"
}

variable "resource_group_name" {
  description = "The name of the resource group where the bot will be created"
  type        = string
}

variable "messaging_endpoint" {
  description = "The HTTPS endpoint where the bot receives messages (e.g., https://api.example.com/api/teams/messages)"
  type        = string
}

variable "key_vault_id" {
  description = "The ID of the Key Vault where bot secrets will be stored"
  type        = string
}

variable "sku" {
  description = "The SKU for the Azure Bot (F0 = Free, S1 = Standard)"
  type        = string
  default     = "F0"
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}
