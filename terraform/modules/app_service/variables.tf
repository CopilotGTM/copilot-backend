variable "app_service_plan_name" {
  description = "Name of the App Service Plan"
  type        = string
}

variable "app_service_name" {
  description = "Name of the App Service (the web app name)"
  type        = string
}

variable "resource_group_name" {
  description = "Resource group for App Service and Plan"
  type        = string
}

variable "location" {
  description = "Azure region"
  type        = string
}

variable "node_version" {
  description = "Node.js version, e.g. '16-lts', used if deploying a Node app. Leave blank for static HTML."
  type        = string
  default     = "16-lts"
}

variable "app_settings" {
  description = "App settings for the App Service"
  type        = map(string)
  default     = {}
}