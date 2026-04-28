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

variable "db_sku_name" {
  description = "The SKU name for the PostgreSQL Flexible Server. Burstable (B_*) does NOT support PgBouncer. Use General Purpose (GP_*) or Memory Optimized (MO_*) for PgBouncer."
  type        = string
  default     = "B_Standard_B1ms"
}

variable "key_vault_id" {
  description = "The ID of the Key Vault to store the database password."
  type        = string
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

# -----------------------------------------------------------------
# PgBouncer Configuration Variables
# -----------------------------------------------------------------
variable "pgbouncer_enabled" {
  description = "Enable PgBouncer connection pooling on the PostgreSQL Flexible Server."
  type        = bool
  default     = false
}

variable "pgbouncer_default_pool_size" {
  description = "Number of server connections per user/database pair for PgBouncer."
  type        = number
  default     = 50
}

variable "pgbouncer_max_client_conn" {
  description = "Maximum number of client connections allowed by PgBouncer."
  type        = number
  default     = 200
}

variable "pgbouncer_pool_mode" {
  description = "PgBouncer pool mode: 'session', 'transaction', or 'statement'. Transaction mode recommended for web apps."
  type        = string
  default     = "transaction"

  validation {
    condition     = contains(["session", "transaction", "statement"], var.pgbouncer_pool_mode)
    error_message = "pgbouncer_pool_mode must be one of: session, transaction, statement."
  }
}