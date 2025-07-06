variable "sp_name" {
  description = "The display name for the service principal."
  type        = string
}

variable "subscription_id" {
  description = "The subscription ID where the service principal will be granted Contributor access."
  type        = string
}