variable "waf_name" {
  description = "Name of the Application Gateway"
  type        = string
}

variable "resource_group_name" {
  description = "Resource Group Name"
  type        = string
}

variable "location" {
  description = "Azure Region"
  type        = string
}

variable "subnet_id" {
  description = "Subnet ID for the Application Gateway. Must be a dedicated subnet."
  type        = string
}

variable "zones" {
  description = "Availability Zones"
  type        = list(string)
  default     = ["1", "2", "3"]
}

variable "min_capacity" {
  description = "Autoscale Minimum Capacity"
  type        = number
  default     = 1
}

variable "max_capacity" {
  description = "Autoscale Maximum Capacity"
  type        = number
  default     = 10
}

variable "tags" {
  description = "Tags to apply to resources"
  type        = map(string)
  default     = {}
}

variable "enable_diagnostics" {
  description = "Enable diagnostic logging for WAF"
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "Number of days to retain logs in Log Analytics"
  type        = number
  default     = 30
}

variable "waf_mode" {
  description = "WAF firewall mode: 'Prevention' blocks malicious requests, 'Detection' only logs them"
  type        = string
  default     = "Detection"
  validation {
    condition     = contains(["Prevention", "Detection"], var.waf_mode)
    error_message = "WAF mode must be either 'Prevention' or 'Detection'."
  }
}
