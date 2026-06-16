variable "env" {
  description = "Environment name (dev, prod)"
  type        = string
}

variable "display_name" {
  description = "Display name for the app registration"
  type        = string
  default     = "CopilotGTM"
}

variable "display_name_override" {
  description = "Optional exact display name for environments that need a specific consent-screen app name"
  type        = string
  default     = null
}

variable "redirect_uris" {
  description = "OAuth callback redirect URIs for this environment"
  type        = list(string)
}

variable "key_vault_id" {
  description = "Key Vault ID to store client credentials"
  type        = string
}

variable "owner_object_id" {
  description = "Object ID that should own the Entra app and service principal. Defaults to the Terraform caller."
  type        = string
  default     = null
}

variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default     = {}
}
