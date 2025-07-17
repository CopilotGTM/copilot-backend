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

variable "docker_image_name" {
  description = "The name of the Docker image to deploy."
  type        = string
}

variable "docker_registry_url" {
  description = "The URL of the Docker registry without protocol (e.g., myregistry.azurecr.io)"
  type        = string
}

variable "acr_id" {
  description = "The ID of the Azure Container Registry"
  type        = string
}

variable "app_settings" {
  description = "App settings for the App Service"
  type        = map(string)
  default     = {}
}

variable "sku_name" {
  description = "The SKU of the App Service Plan."
  type        = string
  default     = "B1"
}

variable "os_type" {
  description = "The operating system for the App Service Plan."
  type        = string
  default     = "Linux"
}

variable "https_only" {
  description = "Whether to enable HTTPS only on the App Service."
  type        = bool
  default     = true
}
