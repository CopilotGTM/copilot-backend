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