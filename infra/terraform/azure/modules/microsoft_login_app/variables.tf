variable "env" {
  description = "Environment name, such as dev or prod"
  type        = string
}

variable "display_name_override" {
  description = "Optional exact Entra app display name"
  type        = string
  default     = null
}

variable "key_vault_id" {
  description = "Key Vault containing the Supabase URL and receiving the login credentials"
  type        = string
}

variable "supabase_url_secret_name" {
  description = "Name of the Key Vault secret containing the environment's Supabase URL"
  type        = string
  default     = "supabase-url"
}

variable "owner_object_id" {
  description = "Object ID that owns the Entra app and service principal; defaults to the Terraform caller"
  type        = string
  default     = null
}

variable "tags" {
  description = "Tags for the Key Vault secrets"
  type        = map(string)
  default     = {}
}
