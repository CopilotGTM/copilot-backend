variable "key_vault_name" {
  description = "The name of the Key Vault."
  type        = string
}

variable "location" {
  description = "The Azure region where the Key Vault should be created."
  type        = string
}

variable "resource_group_name" {
  description = "The resource group in which to create the Key Vault."
  type        = string
}

variable "tenant_id" {
  description = "The Azure AD tenant ID."
  type        = string
}

variable "admin_object_id" {
  description = "The Azure AD object ID for the admin user/service principal with access to the Key Vault."
  type        = string
}

variable "tags" {
  description = "A map of tags to assign to the Key Vault."
  type        = map(string)
  default     = {}
}

variable "enable_rbac_authorization" {
  description = "Enable RBAC authorization for the Key Vault."
  type        = bool
  default     = true
}

variable "public_network_access_enabled" {
  description = "Whether public network access is enabled for the Key Vault. If private endpoints are enabled, this is forced to false."
  type        = bool
  default     = false

  validation {
    condition     = var.public_network_access_enabled == false
    error_message = "Public network access must be disabled for SOC2 private-access controls. Set public_network_access_enabled=false."
  }
}

variable "private_endpoint_enabled" {
  description = "Whether to create a Private Endpoint for the Key Vault."
  type        = bool
  default     = true

  validation {
    condition     = var.private_endpoint_enabled == true
    error_message = "Private Link is mandatory for this Key Vault module. Set private_endpoint_enabled=true."
  }
}

variable "private_endpoint_subnet_id" {
  description = "Subnet ID where the Key Vault Private Endpoint should be created (required when private_endpoint_enabled=true)."
  type        = string
  default     = null

  validation {
    condition     = var.private_endpoint_subnet_id != null && trimspace(var.private_endpoint_subnet_id) != ""
    error_message = "private_endpoint_subnet_id is required (Private Endpoint is mandatory)."
  }
}

variable "private_dns_zone_id" {
  description = "Private DNS Zone ID for Key Vault Private Link (privatelink.vaultcore.azure.net). If provided, a zone group is attached to the private endpoint."
  type        = string
  default     = null

  validation {
    condition     = var.private_dns_zone_id != null && trimspace(var.private_dns_zone_id) != ""
    error_message = "private_dns_zone_id is required so clients resolve the vault name to the Private Endpoint."
  }
}

variable "private_endpoint_name" {
  description = "Optional name override for the Key Vault Private Endpoint."
  type        = string
  default     = ""
}
