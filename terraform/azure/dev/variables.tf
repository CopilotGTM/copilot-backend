# -----------------------------------------------------------------
# General
# -----------------------------------------------------------------

variable "project_name" {
  description = "The project tag to apply across resources."
  type        = string
}

variable "env" {
  description = "Environment identifier (e.g., dev, prod, staging)."
  type        = string
  default     = "dev"
}

variable "location" {
  description = "The Azure region for the Resource Group."
  type        = string
  default     = "EAST US 2"
}

# -----------------------------------------------------------------
# Azure Subscription & Resource Group
# -----------------------------------------------------------------

variable "subscription_id" {
  description = "The Azure subscription ID to use. Required for the azurerm provider."
  type        = string
}

variable "resource_group_name" {
  description = "The base name of the Azure Resource Group to create (the environment will be suffixed)."
  type        = string
}

# -----------------------------------------------------------------
# Networking
# -----------------------------------------------------------------

variable "vnet_name" {
  description = "The name of the Azure Virtual Network to create."
  type        = string
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

# -----------------------------------------------------------------
# Azure Kubernetes Service (AKS)
# -----------------------------------------------------------------

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

variable "aks_os_disk_size_gb" {
  description = "The size of the OS disk in GB for each node in the agent pool."
  type        = number
  default     = 128
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

# -----------------------------------------------------------------
# Azure Container Registry (ACR)
# -----------------------------------------------------------------

variable "acr_name" {
  description = "The name of the Azure Container Registry to create."
  type        = string
}

variable "acr_type" {
  description = "The SKU type of the Azure Container Registry (e.g., Basic, Standard, Premium)."
  type        = string
  default     = "Basic"
}

# -----------------------------------------------------------------
# Azure Cache for Redis
# -----------------------------------------------------------------

variable "redis_name" {
  description = "The name of the Azure Redis instance to create. May include a random or environment-specific suffix."
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

# -----------------------------------------------------------------
# Frontend App Service
# -----------------------------------------------------------------

variable "frontend_app_service_plan_name" {
  description = "The name of the App Service Plan for the frontend app."
  type        = string
}

variable "frontend_app_service_name" {
  description = "The name of the Frontend App Service."
  type        = string
}

variable "frontend_docker_image_name" {
  description = "The name of the Docker image for the frontend app."
  type        = string
  default     = "copilotgtm-frontend-app"
}

variable "frontend_docker_image_tag" {
  description = "The tag of the Docker image for the frontend app."
  type        = string
  default     = "latest"
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

# -----------------------------------------------------------------
# Frontend Admin App Service
# -----------------------------------------------------------------

variable "frontend_admin_app_service_plan_name" {
  description = "The name of the App Service Plan for the frontend admin app."
  type        = string
}

variable "frontend_admin_app_service_name" {
  description = "The name of the Frontend Admin App Service."
  type        = string
}

variable "frontend_admin_docker_image_name" {
  description = "The name of the Docker image for the frontend admin app."
  type        = string
  default     = "copilotgtm-frontend-admin-app"
}

variable "frontend_admin_docker_image_tag" {
  description = "The tag of the Docker image for the frontend admin app."
  type        = string
  default     = "latest"
}

variable "frontend_admin_sku_name" {
  description = "The SKU of the App Service Plan for the frontend admin."
  type        = string
  default     = "B1"
}

variable "frontend_admin_os_type" {
  description = "The OS type of the App Service Plan for the frontend admin."
  type        = string
  default     = "Linux"
}

variable "frontend_admin_https_only" {
  description = "Whether to enable HTTPS only on the App Service for the frontend admin."
  type        = bool
  default     = true
}

# -----------------------------------------------------------------
# Azure Storage
# -----------------------------------------------------------------

variable "storage_account_name" {
  description = "Base name for the Azure Storage Account resource (without env suffix)."
  type        = string
}

# -----------------------------------------------------------------
# Azure DevOps & Pipelines
# -----------------------------------------------------------------

variable "org_service_url" {
  description = "The URL of your Azure DevOps organization (e.g. https://dev.azure.com/yourorg)."
  type        = string
}

variable "devops_project_name" {
  description = "Azure DevOps project name used for pipeline creation (should match your DevOps project)."
  type        = string
}

variable "devops_environment_name" {
  description = "Name of the Azure DevOps Deployment Environment (e.g., 'development', 'uat', 'production'). Used by azuredevops_environment resource."
  type        = string
}

variable "azuredevops_pat" {
  description = "Personal Access Token for Azure DevOps automation. Pass via environment variable or .tfvars (DO NOT hardcode in source for security)."
  type        = string
  sensitive   = true
}

variable "sp_name" {
  description = "The display name for the service principal for Azure DevOps."
  type        = string
}

variable "azuredevops_sp_display_name" {
  description = "Display name of the Azure AD service principal used by the Azure DevOps service connection for the pipeline. Find this in Azure AD > App registrations."
  type        = string
}

# -----------------------------------------------------------------
# Backend Pipeline
# -----------------------------------------------------------------

variable "backend_repo_name" {
  description = "Azure DevOps repository name containing the YAML pipeline definition."
  type        = string
}

variable "backend_pipeline_name" {
  description = "Name of the Azure DevOps pipeline resource. Cannot contain '/' or special characters."
  type        = string
}

variable "backend_yml_path" {
  description = "Relative path (from repo root) to the Azure Pipelines YAML file."
  type        = string
  default     = "azure-pipelines.yml"
}

variable "backend_branch" {
  description = "Branch for backend pipeline"
  type        = string
  default     = "dev"
}

# -----------------------------------------------------------------
# Knowledge Base Pipeline
# -----------------------------------------------------------------

variable "knowledge_base_repo_name" {
  description = "Azure DevOps repository name containing the YAML pipeline definition for the knowledge base."
  type        = string
  default     = "knowledge-base"
}

variable "knowledge_base_pipeline_name" {
  description = "Name of the Azure DevOps pipeline resource for the knowledge base. Cannot contain '/' or special characters."
  type        = string
  default     = "knowledge-base-pipeline"
}

variable "knowledge_base_yml_path" {
  description = "Relative path (from repo root) to the Azure Pipelines YAML file for the knowledge base."
  type        = string
  default     = "azure-pipelines.yml"
}

variable "knowledge_base_branch" {
  description = "Branch of Repo for knowledge base"
  type        = string
  default     = "dev"
}

# -----------------------------------------------------------------
# Frontend Pipeline
# -----------------------------------------------------------------

variable "frontend_repo_name" {
  description = "Azure DevOps repository name containing the YAML pipeline definition for the frontend."
  type        = string
}

variable "frontend_pipeline_name" {
  description = "Name of the Azure DevOps pipeline resource for the frontend. Cannot contain '/' or special characters."
  type        = string
}

variable "frontend_yml_path" {
  description = "Relative path (from repo root) to the Azure Pipelines YAML file for the frontend."
  type        = string
  default     = "azure-pipelines.yml"
}

variable "frontend_branch" {
  description = "Branch of Repo"
  type        = string
  default     = "dev"
}

# -----------------------------------------------------------------
# Frontend Admin Pipeline
# -----------------------------------------------------------------

variable "frontend_admin_repo_name" {
  description = "Azure DevOps repository name containing the YAML pipeline definition for the frontend admin."
  type        = string
}

variable "frontend_admin_pipeline_name" {
  description = "Name of the Azure DevOps pipeline resource for the frontend admin. Cannot contain '/' or special characters."
  type        = string
}

variable "frontend_admin_yml_path" {
  description = "Relative path (from repo root) to the Azure Pipelines YAML file for the frontend admin."
  type        = string
  default     = "azure-pipelines.yml"
}

variable "frontend_admin_branch" {
  description = "Branch of Repo for frontend admin"
  type        = string
  default     = "dev"
}

variable "frontend_admin_dns_a_record_name" {
  description = "The name of the A record for the frontend admin app service."
  type        = string
  default     = "devadmin"
}

# -----------------------------------------------------------------
# Static DevOps Agent VM
# -----------------------------------------------------------------

variable "agent_name" {
  description = "Name to assign to this DevOps agent instance."
  type        = string
}

variable "agent_pool" {
  description = "Name of the Azure DevOps agent pool to register this agent in."
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

# -----------------------------------------------------------------
# DNS
# -----------------------------------------------------------------

variable "dns_zone_name" {
  description = "The name of the DNS zone to use."
  type        = string
}

variable "dns_zone_resource_group" {
  description = "The name of the resource group where the DNS zone is located."
  type        = string
}

variable "frontend_dns_a_record_name" {
  description = "The name of the A record for the frontend app service."
  type        = string
  default     = "devapp"
}

# -----------------------------------------------------------------
# Secrets & Certificates
# -----------------------------------------------------------------

variable "db_password" {
  description = "Database password"
  type        = string
  default     = ""
}

variable "azure_openai_key" {
  description = "Database password"
  type        = string
  default     = ""
}

variable "letsencrypt_email" {
  description = "Email address for Let's Encrypt registration"
  type        = string
}

variable "certificates" {
  description = "Map of certificates to create"
  type = map(object({
    name        = string
    namespace   = string
    secret_name = string
    issuer_name = string
    issuer_kind = string
    common_name = string
    dns_names   = list(string)
  }))
  default = {}
}

# -----------------------------------------------------------------
# Supabase Variables
# -----------------------------------------------------------------

variable "super_admin_email" {
  description = "Email address for the super admin user."
  type        = string
}

variable "supabase_url" {
  description = "URL for the Supabase instance."
  type        = string
}

variable "supabase_anon_key" {
  description = "API key for the Supabase instance."
  type        = string
  sensitive   = true
}

variable "supabase_service_role_key" {
  description = "Service role key for the Supabase instance."
  type        = string
  sensitive   = true
}

variable "supabase_jwt_secret" {
  description = "JWT secret for the Supabase instance."
  type        = string
  sensitive   = true
}

# -----------------------------------------------------------------
# Resend API Key
# -----------------------------------------------------------------

variable "resend_api_key" {
  description = "API key for the Resend service."
  type        = string
  sensitive   = true
}

variable "resend_email" {
  description = "Email address for the Resend service."
  type        = string
  default     = "noreply@team.copilotgtm.com"
}
