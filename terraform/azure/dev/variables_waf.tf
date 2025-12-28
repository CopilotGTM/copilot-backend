variable "vnet_waf_subnet" {
  description = "CIDR for the Application Gateway subnet."
  type        = string
  default     = "10.0.5.0/24"
}

variable "waf_name" {
  description = "Name of the Application Gateway"
  type        = string
  default     = "copilot-gtm-waf"
}
