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

variable "aks_os_disk_size_gb" {
  description = "The size of the OS disk in GB for each node in the agent pool."
  type        = number
  default     = 128
}

variable "vnet_name" {
  description = "The name of the Azure Virtual Network to create."
  type        = string
}

variable "acr_name" {
  description = "The name of the Azure Container Registry to create."
  type        = string
}

variable "acr_type" {
  description = "The SKU type of the Azure Container Registry (e.g., Basic, Standard, Premium)."
  type        = string
  default     = "Basic"
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

variable "frontend_node_version" {
  description = "Node.js version to use in the frontend App Service (e.g., '20-lts')."
  type        = string
  default     = "20-lts"
}

variable "project_name" {
  description = "The project tag to apply across resources."
  type        = string
}
variable "storage_account_name" {
  description = "Base name for the Azure Storage Account resource (without env suffix)."
  type        = string
}

variable "mongodb_account_name" {
  description = "Base name for the Azure CosmosDB (MongoDB API) account (without env suffix)."
  type        = string
}
# Project and repository for pipeline creation in Azure DevOps

variable "devops_project_name" {
  description = "Azure DevOps project name used for pipeline creation (should match your DevOps project)."
  type        = string
}
# Azure DevOps deployment environment name for pipeline deployments
variable "devops_environment_name" {
  description = "Name of the Azure DevOps Deployment Environment (e.g., 'development', 'uat', 'production'). Used by azuredevops_environment resource."
  type        = string
}

variable "backend_repo_name" {
  description = "Azure DevOps repository name containing the YAML pipeline definition."
  type        = string
}
# Azure DevOps organization (ORG) URL for provider (e.g. https://dev.azure.com/yourorg)
variable "org_service_url" {
  description = "The URL of your Azure DevOps organization (e.g. https://dev.azure.com/yourorg)."
  type        = string
}
# Azure DevOps PAT variable (never assign value here!)
variable "azuredevops_pat" {
  description = "Personal Access Token for Azure DevOps automation. Pass via environment variable or .tfvars (DO NOT hardcode in source for security)."
  type        = string
  sensitive   = true
}
# Pipeline name for Azure DevOps build definition. Must NOT contain '/' or invalid characters.
variable "backend_pipeline_name" {
  description = "Name of the Azure DevOps pipeline resource. Cannot contain '/' or special characters."
  type        = string
}

# YAML path for Azure DevOps pipeline definition file
variable "backend_yml_path" {
  description = "Relative path (from repo root) to the Azure Pipelines YAML file."
  type        = string
  default     = "azure-pipelines.yml"
}

# Display name of the Azure DevOps pipeline service principal (used for automatic lookup)
variable "azuredevops_sp_display_name" {
  description = "Display name of the Azure AD service principal used by the Azure DevOps service connection for the pipeline. Find this in Azure AD > App registrations."
  type        = string
}

variable "admin_username" {
  description = "Admin username for the static DevOps agent VM."
  type        = string
  default     = "azureuser"
}

variable "admin_password" {
  description = "Admin password for the static DevOps agent VM."
  type        = string
  sensitive   = true
}

# Static DevOps Agent Auto-Registration Variables

variable "agent_pool" {
  description = "Name of the Azure DevOps agent pool to register this agent in."
  type        = string
}

variable "agent_name" {
  description = "Name to assign to this DevOps agent instance."
  type        = string
}

variable "redis_sku_name" {
  description = "The SKU of the Redis cache."
  type        = string
  default     = "Standard"
}

variable "redis_family" {
  description = "The SKU family of the Redis cache."
  type        = string
  default     = "C"
}

variable "redis_capacity" {
  description = "The capacity of the Redis cache."
  type        = number
  default     = 0
}

variable "frontend_sku_name" {
  description = "The SKU of the App Service Plan for the frontend."
  type        = string
  default     = "B1"
}

variable "frontend_os_type" {
  description = "The OS type of the App Service Plan for the frontend."
  type        = string
  default     = "Linux"
}

variable "frontend_https_only" {
  description = "Whether to enable HTTPS only on the App Service for the frontend."
  type        = bool
  default     = true
}