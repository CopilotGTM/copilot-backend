# -----------------------------------------------------------------------------
# Managed DevOps Pool Module - Variables
# -----------------------------------------------------------------------------

variable "pool_name" {
  description = "Name of the Managed DevOps Pool and Azure DevOps agent pool."
  type        = string
}

variable "location" {
  description = "Azure region for the Managed DevOps Pool. Must match the delegated subnet region."
  type        = string
}

variable "resource_group_id" {
  description = "Resource group ID that owns the Managed DevOps Pool resource."
  type        = string
}

variable "dev_center_project_resource_id" {
  description = "Azure Dev Center project resource ID required by Managed DevOps Pools."
  type        = string
}

variable "ado_org_url" {
  description = "Azure DevOps organization URL, for example https://dev.azure.com/copilotgtm."
  type        = string
}

variable "ado_project_names" {
  description = "Azure DevOps projects allowed to use this pool. Empty list means all projects in the organization."
  type        = list(string)
  default     = []
}

variable "parallelism" {
  description = "Azure DevOps organization parallelism exposed by this pool."
  type        = number
  default     = 1
}

variable "maximum_concurrency" {
  description = "Maximum number of agents the Managed DevOps Pool can provision."
  type        = number
  default     = 1
}

variable "subnet_id" {
  description = "Dedicated subnet ID delegated to Microsoft.DevOpsInfrastructure/pools."
  type        = string
}

variable "sku_name" {
  description = "Azure VM SKU for pool agents."
  type        = string
  default     = "Standard_D2s_v5"
}

variable "image_name" {
  description = "Managed DevOps Pool well-known image alias."
  type        = string
  default     = "ubuntu-24.04-g2"
}

variable "image_alias" {
  description = "Friendly alias for ImageOverride demands."
  type        = string
  default     = "ubuntu-24.04-g2"
}

variable "image_buffer" {
  description = "Standby buffer for the image. Use 0 for cost/quota-sensitive pools and higher values only when warm capacity is required."
  type        = string
  default     = "100"
}

variable "agent_state" {
  description = "Agent lifecycle mode. Use Stateful for fast dev cache reuse and Stateless for prod."
  type        = string
  default     = "Stateless"

  validation {
    condition     = contains(["Stateful", "Stateless"], var.agent_state)
    error_message = "agent_state must be Stateful or Stateless."
  }
}

variable "stateful_grace_period" {
  description = "How long a stateful agent waits for another job before shutdown."
  type        = string
  default     = "00:30:00"
}

variable "stateful_max_lifetime" {
  description = "Maximum continuous lifetime for stateful agents."
  type        = string
  default     = "1.00:00:00"
}

variable "prediction_preference" {
  description = "Optional automatic standby preference. Empty disables automatic predictions."
  type        = string
  default     = ""

  validation {
    condition = contains([
      "",
      "Balanced",
      "BestPerformance",
      "MoreCostEffective",
      "MorePerformance",
      "MostCostEffective",
    ], var.prediction_preference)
    error_message = "prediction_preference must be empty or a supported Managed DevOps Pools prediction preference."
  }
}

variable "data_disk_size_gib" {
  description = "Optional data disk size for build/Docker cache. Set 0 to omit."
  type        = number
  default     = 64
}

variable "data_disk_storage_account_type" {
  description = "Storage account type for the optional data disk."
  type        = string
  default     = "StandardSSD_LRS"
}

variable "os_disk_storage_account_type" {
  description = "Storage account type for the agent OS disk."
  type        = string
  default     = "StandardSSD"
}

variable "work_folder" {
  description = "Agent work folder path."
  type        = string
  default     = "/mnt/vss/_work"
}

variable "tags" {
  description = "Tags to apply to the Managed DevOps Pool."
  type        = map(string)
  default     = {}
}
