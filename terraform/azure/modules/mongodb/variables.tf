variable "mongodb_account_name" {
  description = "The Cosmos DB account name (unique, 3-44 chars, lowercase alphanumeric and - only, must begin/end with letter or number)."
  type        = string
}

variable "resource_group_name" {
  description = "The name of the Resource Group in which to create the MongoDB account."
  type        = string
}

variable "location" {
  description = "The Azure region where the MongoDB account will be created."
  type        = string
}

variable "tags" {
  description = "A map of tags to assign to the MongoDB account."
  type        = map(string)
  default     = {}
}