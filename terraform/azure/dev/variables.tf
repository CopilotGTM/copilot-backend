variable "env" {
  description = "Environment identifier (e.g., dev, prod, staging)."
  type        = string
  default     = "dev"
}

variable "resource_group_name" {
  description = "The base name of the Azure Resource Group to create (the environment will be suffixed)."
  type        = string
}

variable "subscription_id" {
  description = "The Azure subscription ID to use. Required for the azurerm provider."
  type        = string
}

variable "location" {
  description = "The Azure region for the Resource Group."
  type        = string
  default     = "East US"
}

variable "vnet_address_space" {
  description = "Address space list for the Virtual Network."
  type        = list(string)
  default     = ["10.0.0.0/16"]
}

variable "vnet_public_subnets" {
  description = "Address prefixes for public subnets (list of CIDRs)."
  type        = list(string)
  default     = ["10.0.1.0/24"]
}

variable "vnet_private_subnets" {
  description = <<-DESC
    Address prefixes for private subnets (list of CIDRs).
    Best practice: Use two:
      - One for AKS/microservices (e.g., 10.0.2.0/24)
      - One for data/stateful (Redis, DB) (e.g., 10.0.3.0/24)
  DESC
  type        = list(string)
  default     = ["10.0.2.0/24", "10.0.3.0/24"]
}

variable "aks_name" {
  description = "The name of the AKS cluster."
  type        = string
}

variable "aks_dns_prefix" {
  description = "The DNS prefix for the AKS cluster."
  type        = string
}

variable "aks_node_count" {
  description = "Node count in the default AKS agent pool."
  type        = number
  default     = 2
}

variable "aks_vm_size" {
  description = "VM size for AKS agent pool."
  type        = string
  default     = "Standard_DS2_v2"
}

variable "aks_service_cidr" {
  description = "Kubernetes service CIDR (must NOT overlap with any VNet or subnet)."
  type        = string
  default     = "10.1.0.0/16"
}

variable "aks_dns_service_ip" {
  description = "Kubernetes DNS service IP (must be inside the service CIDR)."
  type        = string
  default     = "10.1.0.10"
}
variable "vnet_name" {
  description = "The name of the Azure Virtual Network to create."
  type        = string
}

variable "acr_name" {
  description = "The name of the Azure Container Registry to create."
  type        = string
}

variable "redis_name" {
  description = "The name of the Azure Redis instance to create. May include a random or environment-specific suffix."
  type        = string
}

variable "frontend_app_service_plan_name" {
  description = "The name of the App Service Plan for the frontend app."
  type        = string
}

variable "frontend_app_service_name" {
  description = "The name of the Frontend App Service."
  type        = string
}

variable "project_name" {
  description = "The project tag to apply across resources."
  type        = string
}