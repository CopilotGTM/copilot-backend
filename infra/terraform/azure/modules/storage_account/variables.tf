variable "storage_account_name" {
  description = "The name of the Storage Account. Must be globally unique, 3-24 lowercase alphanumeric characters."
  type        = string
}

variable "resource_group_name" {
  description = "The name of the Resource Group in which to create the Storage Account."
  type        = string
}

variable "location" {
  description = "The Azure region where the Storage Account will be created."
  type        = string
}

variable "account_tier" {
  description = "Defines the Tier to use for this storage account (Standard or Premium)."
  type        = string
  default     = "Standard"
}

variable "account_replication_type" {
  description = "Defines the type of replication to use for this storage account (LRS, GRS, RAGRS, ZRS)."
  type        = string
  default     = "LRS"
}

variable "tags" {
  description = "A map of tags to assign to the resource."
  type        = map(string)
  default     = {}
}

variable "public_network_access_enabled" {
  description = "Whether public network access is enabled for the Storage Account. If private endpoints are enabled, this is forced to false."
  type        = bool
  default     = true
}

variable "allow_nested_items_to_be_public" {
  description = "Whether containers/blobs can be made public. Keep false for private customer data storage."
  type        = bool
  default     = false
}

variable "shared_access_key_enabled" {
  description = "Whether Shared Key authorization is enabled. Keep false when workloads use Entra ID and user delegation SAS."
  type        = bool
  default     = false
}

variable "blob_delete_retention_days" {
  description = "Soft-delete retention in days for blobs. Set null to disable."
  type        = number
  default     = 7
}

variable "container_delete_retention_days" {
  description = "Soft-delete retention in days for containers. Set null to disable."
  type        = number
  default     = 7
}

variable "blob_versioning_enabled" {
  description = "Whether blob versioning is enabled for recovery from overwrites/deletes."
  type        = bool
  default     = true
}

variable "blob_version_retention_days" {
  description = "Number of days to retain old blob versions via lifecycle policy. Set null to disable version cleanup."
  type        = number
  default     = 30
}

variable "blob_diagnostics_enabled" {
  description = "Whether to send Blob service read/write/delete logs and transaction metrics to Log Analytics."
  type        = bool
  default     = false
}

variable "log_analytics_workspace_id" {
  description = "Log Analytics Workspace ID for Blob service diagnostic logs."
  type        = string
  default     = null
}

variable "private_endpoint_enabled" {
  description = "Whether to create a Private Endpoint for the Storage Account."
  type        = bool
  default     = false
}

variable "private_endpoint_subnet_id" {
  description = "Subnet ID where the Storage Account Private Endpoint should be created (required when private_endpoint_enabled=true)."
  type        = string
  default     = null
}

variable "private_endpoint_subresource_names" {
  description = "Storage subresources for the Private Endpoint (e.g., blob, file, queue, table)."
  type        = list(string)
  default     = ["blob"]
}

variable "private_dns_zone_ids" {
  description = "Private DNS Zone IDs to attach via the Private Endpoint zone group (e.g., privatelink.blob.core.windows.net)."
  type        = list(string)
  default     = null
}

# Optional list of allowed origins for a single blob service CORS rule.
# If empty, no CORS rule will be created.
variable "allowed_blob_cors_origins" {
  description = "List of allowed origins for blob service CORS. Leave empty to disable CORS configuration."
  type        = list(string)
  default     = []
}
