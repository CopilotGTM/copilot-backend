variable "env" {
  description = "The environment name (e.g., dev, prod)."
  type        = string
}

variable "resource_group_name" {
  description = "The name of the resource group."
  type        = string
}

variable "location" {
  description = "The Azure region for the resources."
  type        = string
}

variable "db_version" {
  description = "PostgreSQL major version."
  type        = string
  default     = "14"
}

variable "db_admin_user" {
  description = "The administrator username for the PostgreSQL server."
  type        = string
}

variable "db_admin_password" {
  description = "The administrator password for the PostgreSQL server."
  type        = string
  sensitive   = true
}

variable "db_name" {
  description = "The base name for the PostgreSQL server (without env suffix)."
  type        = string
  default     = ""
}

variable "store_password_in_key_vault" {
  description = "When true, store the DB admin password in Key Vault as a secret (requires Key Vault data-plane access; for private-only vaults this must run from inside the VNet)."
  type        = bool
  default     = false
}

variable "key_vault_id" {
  description = "The ID of the Key Vault to store the database password (required when store_password_in_key_vault=true)."
  type        = string
  default     = null

  validation {
    condition     = var.store_password_in_key_vault == false || (var.key_vault_id != null && trimspace(var.key_vault_id) != "")
    error_message = "key_vault_id must be set when store_password_in_key_vault=true."
  }
}

variable "db_password_secret_name" {
  description = "Key Vault secret name to use when store_password_in_key_vault=true."
  type        = string
  default     = "db-admin-password"
}


variable "ci_cd_agent_ip_start" {
  description = "The starting IP address for the CI/CD agent firewall rule."
  type        = string
  default     = ""
}

variable "ci_cd_agent_ip_end" {
  description = "The ending IP address for the CI/CD agent firewall rule."
  type        = string
  default     = ""
}

variable "tags" {
  description = "A map of tags to assign to the resources."
  type        = map(string)
  default     = {}
}