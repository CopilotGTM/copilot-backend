variable "waf_subnet_prefix" {
  description = "Optional CIDR prefix for a dedicated Application Gateway subnet. If empty, no dedicated subnet is created."
  type        = string
  default     = ""
}
