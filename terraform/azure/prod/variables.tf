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
  default     = "prod"
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
  default     = "copilot-gtm-vnet"
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

variable "vnet_private_endpoints_subnet" {
  description = "CIDR for a dedicated private endpoints subnet (used for Private Link endpoints)."
  type        = string
  default     = "10.0.4.0/24"
}

variable "enable_key_vault_private_endpoints" {
  description = "Enable Private Link private endpoints for Key Vaults and disable their public network access. This is mandatory for the Key Vault module."
  type        = bool
  default     = true
}

variable "enable_storage_private_endpoints" {
  description = "Enable Private Link private endpoints for Storage Accounts and disable their public network access. Keep false until workloads are confirmed to access Storage via VNet + Private DNS."
  type        = bool
  default     = true
}

# -----------------------------------------------------------------
# Azure Kubernetes Service (AKS)
# -----------------------------------------------------------------

variable "aks_name" {
  description = "The name of the AKS cluster."
  type        = string
  default     = "copilot-gtm-aks"
}

variable "aks_dns_prefix" {
  description = "The DNS prefix for the AKS cluster."
  type        = string
  default     = "aks"
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

variable "aks_main_app_namespace" {
  description = "The main Kubernetes namespace for deploying applications in AKS."
  type        = string
  default     = "default"
}

variable "aks_main_app_service_account" {
  description = "The main Kubernetes service account for deploying applications in AKS."
  type        = string
  default     = "default"
}

variable "aks_workers_namespace" {
  description = "Namespace where worker workloads run."
  type        = string
  default     = "default"
}

variable "aks_workers_service_account" {
  description = "Service account name shared by all workers."
  type        = string
  default     = "copilot-workers"
}
# -----------------------------------------------------------------
# Azure Container Registry (ACR)
# -----------------------------------------------------------------

variable "acr_name" {
  description = "The name of the Azure Container Registry to create."
  type        = string
  default     = "copilotgtmacr"
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
  default     = "copilot-gtm-redis"
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
  default     = "copilot-gtm-frontend-asp"
}

variable "frontend_app_service_name" {
  description = "The name of the Frontend App Service."
  type        = string
  default     = "copilot-gtm-frontend-app"
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
  default     = "copilot-gtm-admin-asp"
}

variable "frontend_admin_app_service_name" {
  description = "The name of the Frontend Admin App Service."
  type        = string
  default     = "copilot-gtm-admin-app"
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
  default     = "copilotgtmstorage"
}

# Additional organisation data storage account (separate logical store)
variable "org_storage_account_name" {
  description = "Base name for the organisation data Storage Account (without env suffix)."
  type        = string
  default     = "organisationstorage"
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
  default     = ""
}

variable "azuredevops_pat_secret_name" {
  description = "Optional: name of an existing Key Vault secret that holds the Azure DevOps PAT. If azuredevops_pat is empty and this is set, Terraform will read the PAT from Key Vault (requires Key Vault access)."
  type        = string
  default     = ""
}

variable "sp_name" {
  description = "The display name for the service principal for Azure DevOps."
  type        = string
  default     = "copilot-gtm-devops-sp"
}

variable "azuredevops_sp_display_name" {
  description = "Display name of the Azure AD service principal used by the Azure DevOps service connection for the pipeline. Find this in Azure AD > App registrations."
  type        = string
  default     = "TerraformSP"
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
  default     = "backend-pipeline"
}

variable "backend_yml_path" {
  description = "Relative path (from repo root) to the Azure Pipelines YAML file."
  type        = string
  default     = "azure-pipelines.yml"
}

variable "backend_branch" {
  description = "Branch for backend pipeline"
  type        = string
  default     = "main"
}

# -----------------------------------------------------------------
# Terraform Pipeline
# -----------------------------------------------------------------

variable "terraform_repo_name" {
  description = "Azure DevOps repository name containing the Terraform code."
  type        = string
}

variable "terraform_pipeline_name" {
  description = "Name of the Azure DevOps pipeline for Terraform."
  type        = string
  default     = "infra-pipeline"
}

variable "infra_variable_group_name" {
  description = "Name of the Azure DevOps variable group for infrastructure secrets."
  type        = string
  default     = "copilot-infra-secrets"
}

variable "terraform_yml_path" {
  description = "Path to the Azure Pipelines YAML file for Terraform."
  type        = string
  default     = ".pipeline/azure-pipeline-prod.yml"
}

variable "terraform_branch" {
  description = "Branch for Terraform pipeline"
  type        = string
  default     = "main"
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
  default     = "frontend-pipeline"
}

variable "frontend_yml_path" {
  description = "Relative path (from repo root) to the Azure Pipelines YAML file for the frontend."
  type        = string
  default     = "azure-pipelines.yml"
}

variable "frontend_branch" {
  description = "Branch of Repo"
  type        = string
  default     = "main"
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
  default     = "frontend-admin-pipeline"
}

variable "frontend_admin_yml_path" {
  description = "Relative path (from repo root) to the Azure Pipelines YAML file for the frontend admin."
  type        = string
  default     = "azure-pipelines.yml"
}

variable "frontend_admin_branch" {
  description = "Branch of Repo for frontend admin"
  type        = string
  default     = "main"
}

variable "frontend_admin_dns_a_record_name" {
  description = "The name of the A record for the frontend admin app service."
  type        = string
  default     = "admin"
}

# -----------------------------------------------------------------
# Static DevOps Agent VM
# -----------------------------------------------------------------

variable "agent_name" {
  description = "Name to assign to this DevOps agent instance."
  type        = string
  default     = "gtm-devops-agent-01"
}

variable "agent_pool" {
  description = "Name of the Azure DevOps agent pool to register this agent in."
  type        = string
  default     = "StaticAgentPool"
}

variable "admin_username" {
  description = "Admin username for the static DevOps agent VM."
  type        = string
  default     = "azureuser"
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
  default     = "app"
}

# -----------------------------------------------------------------
# Secrets & Certificates
# -----------------------------------------------------------------



variable "db_location" {
  description = "The Azure region for the Database."
  type        = string
  default     = "eastus"
}

variable "db_name" {
  description = "The name of the Database."
  type        = string
  default     = "copilotgtm-postgresdb"
}

variable "db_sku_name" {
  description = "The SKU name for PostgreSQL. Use GP_* (General Purpose) for PgBouncer support."
  type        = string
  default     = "B_Standard_B1ms"
}

# -----------------------------------------------------------------
# PgBouncer Configuration (requires General Purpose tier or higher)
# -----------------------------------------------------------------
variable "pgbouncer_enabled" {
  description = "Enable PgBouncer connection pooling. Requires GP_* or MO_* SKU."
  type        = bool
  default     = false
}

variable "pgbouncer_default_pool_size" {
  description = "Number of server connections per user/database pair."
  type        = number
  default     = 50
}

variable "pgbouncer_max_client_conn" {
  description = "Maximum number of client connections allowed."
  type        = number
  default     = 200
}

variable "pgbouncer_pool_mode" {
  description = "Pool mode: session, transaction, or statement."
  type        = string
  default     = "transaction"
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
  default     = ""
}

variable "supabase_url" {
  description = "URL for the Supabase instance."
  type        = string
  default     = ""
}

variable "supabase_anon_key" {
  description = "API key for the Supabase instance."
  type        = string
  sensitive   = true
  default     = ""
}

variable "supabase_service_role_key" {
  description = "Service role key for the Supabase instance."
  type        = string
  sensitive   = true
  default     = ""
}

variable "supabase_jwt_secret" {
  description = "JWT secret for the Supabase instance."
  type        = string
  sensitive   = true
  default     = ""
}

# -----------------------------------------------------------------
# Resend API Key
# -----------------------------------------------------------------

variable "resend_api_key" {
  description = "API key for the Resend service."
  type        = string
  sensitive   = true
  default     = ""
}

variable "resend_email" {
  description = "Email address for the Resend service."
  type        = string
  default     = "noreply@team.copilotgtm.com"
}

# -----------------------------------------------------------------
# KEDA Configuration
# -----------------------------------------------------------------

variable "enable_keda_autoscaling" {
  description = "Enable KEDA-based autoscaling for applications"
  type        = bool
  default     = false
}

variable "keda_polling_interval" {
  description = "KEDA polling interval in seconds"
  type        = number
  default     = 30
}

variable "keda_cooldown_period" {
  description = "KEDA cooldown period in seconds"
  type        = number
  default     = 300
}

# -----------------------------------------------------------------
# GPU Knowledge Worker Node Pool (AKS optional)
# -----------------------------------------------------------------
variable "enable_gpu_worker_pool" {
  description = "Whether to create a dedicated GPU node pool for knowledge workers"
  type        = bool
  default     = false
}

variable "gpu_node_pool_name" {
  description = "Name for the GPU node pool (1-12 chars)"
  type        = string
  default     = "kgpu"
}

variable "gpu_vm_size" {
  description = "VM size for GPU nodes"
  type        = string
  default     = "Standard_NC4as_T4_v3"
}

variable "gpu_min_count" {
  description = "Minimum GPU node count (0 for scale to zero if autoscaling is later enabled)"
  type        = number
  default     = 0
}

variable "gpu_max_count" {
  description = "Maximum GPU node count (used only if autoscaling enabled in future)"
  type        = number
  default     = 1
}

variable "gpu_node_labels" {
  description = "Map of node labels for GPU pool"
  type        = map(string)
  default = {
    workload = "knowledge-gpu"
  }
}

variable "gpu_node_taints" {
  description = "List of taints applied to GPU nodes"
  type        = list(string)
  default     = ["dedicated=gpu:NoSchedule"]
}

# -----------------------------------------------------------------
# Nango Config 
# ----------------------------------------------------------------
variable "nango_secret" {
  description = "Nango Secret Key"
  type        = string
  default     = ""
}

# -----------------------------------------------------------------
# K8s Addons Variables (Migrated from k8s/variables.tf)
# -----------------------------------------------------------------

variable "azure_dns_managed_identity_client_id" {
  type        = string
  description = "Client ID of the managed identity used by cert-manager to solve Azure DNS challenges"
  default     = "" # Will be populated from AKS module output if not provided
}

variable "api_subdomain" {
  type        = string
  description = "API subdomain (e.g. api, devapi)"
  default     = "api"
}

variable "app_namespace" {
  type    = string
  default = "default"
}

# KEDA Configuration
variable "keda_namespace" {
  type        = string
  description = "Namespace for KEDA system components"
  default     = "keda-system"
}

# -----------------------------------------------------------------
# WAF Variables
# -----------------------------------------------------------------

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

