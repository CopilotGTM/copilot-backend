variable "env" {
  description = "Environment name (dev, prod)"
  type        = string
}

variable "display_name" {
  description = "Display name for the app registration"
  type        = string
  default     = "CopilotGTM"
}

variable "redirect_uris" {
  description = "OAuth callback redirect URIs for this environment"
  type        = list(string)
}

variable "key_vault_id" {
  description = "Key Vault ID to store client credentials"
  type        = string
}

variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default     = {}
}
