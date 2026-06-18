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

variable "enable_storage_diagnostics" {
  description = "Enable Blob service diagnostic logs for storage accounts."
  type        = bool
  default     = true
}

variable "storage_diagnostics_retention_days" {
  description = "Log Analytics retention in days for storage diagnostics."
  type        = number
  default     = 30
}

variable "storage_diagnostics_daily_quota_gb" {
  description = "Daily ingestion cap in GB for storage diagnostics Log Analytics workspace."
  type        = number
  default     = 1
}

variable "enable_postgresql_private_endpoint" {
  description = "Enable Private Link for PostgreSQL."
  type        = bool
  default     = false
}

variable "postgresql_public_network_access_enabled" {
  description = "Enable public network access for PostgreSQL. Set false only after Private Link connectivity is verified."
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
  description = "Node count in the default AKS agent pool (used when autoscaling is disabled)."
  type        = number
  default     = 2
}

variable "aks_enable_auto_scaling" {
  description = "Enable autoscaling on the default AKS node pool."
  type        = bool
  default     = false
}

variable "aks_min_node_count" {
  description = "Minimum number of nodes when autoscaling is enabled."
  type        = number
  default     = 1
}

variable "aks_max_node_count" {
  description = "Maximum number of nodes when autoscaling is enabled."
  type        = number
  default     = 3
}

variable "aks_vm_size" {
  description = "VM size for AKS agent pool."
  type        = string
  default     = "Standard_DS2_v2"
}

variable "aks_max_pods" {
  description = "Maximum number of pods that can run on each AKS default node pool node."
  type        = number
  default     = null
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

variable "aks_outbound_type" {
  description = "AKS outbound egress type."
  type        = string
  default     = "loadBalancer"
}

variable "aks_existing_principal_id" {
  description = "Existing AKS control-plane managed identity principal ID used to grant network permissions before AKS updates."
  type        = string
  default     = null
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
  default     = "copilot-backend-pipeline"
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
# Terraform Pipeline
# -----------------------------------------------------------------

variable "terraform_repo_name" {
  description = "Azure DevOps repository name containing the Terraform code."
  type        = string
}

variable "terraform_pipeline_name" {
  description = "Name of the Azure DevOps pipeline for Terraform."
  type        = string
  default     = "copilot-infra-pipeline"
}

variable "infra_variable_group_name" {
  description = "Name of the Azure DevOps variable group for infrastructure secrets."
  type        = string
  default     = "copilot-infra-secrets"
}

variable "terraform_yml_path" {
  description = "Path to the Azure Pipelines YAML file for Terraform."
  type        = string
  default     = ".azure-pipelines/azure-infra-pipeline-dev.yml"
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
  default     = "copilot-frontend-pipeline"
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
# Managed DevOps Pools (target elastic agent model)
# -----------------------------------------------------------------

variable "enable_managed_devops_pools" {
  description = "Create Managed DevOps Pools for elastic Azure DevOps agents. Keep false until prerequisites and subnets are ready."
  type        = bool
  default     = false
}

variable "managed_devops_dev_center_project_resource_id" {
  description = "Existing Azure Dev Center project resource ID required by Managed DevOps Pools. Leave empty to create one in this stack."
  type        = string
  default     = ""
}

variable "managed_devops_dev_center_name" {
  description = "Azure Dev Center name to create when managed_devops_dev_center_project_resource_id is empty."
  type        = string
  default     = "cgtm-devops-devcenter-dev"
}

variable "managed_devops_project_name" {
  description = "Azure Dev Center project name to create when managed_devops_dev_center_project_resource_id is empty."
  type        = string
  default     = "copilot-devops-project-dev"
}

variable "managed_devops_pools_subnets" {
  description = "Dedicated subnets for Managed DevOps Pools. Each pool needs its own delegated subnet."
  type = list(object({
    name           = string
    address_prefix = string
  }))
  default = []
}

variable "managed_devops_dev_fast_pool_name" {
  description = "Managed DevOps Pool for fast dev/PR validation."
  type        = string
  default     = "copilot-devops-dev-fast"
}

variable "managed_devops_dev_deploy_pool_name" {
  description = "Managed DevOps Pool for dev deployments."
  type        = string
  default     = "copilot-devops-dev-deploy"
}

variable "managed_devops_pool_sku_name" {
  description = "VM SKU used by Managed DevOps Pool agents."
  type        = string
  default     = "Standard_D2s_v5"
}

variable "managed_devops_image_name" {
  description = "Managed DevOps Pool well-known image alias."
  type        = string
  default     = "ubuntu-24.04-g2"
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
  description = "The SKU name for PostgreSQL. Burstable (B_*) for dev, GP_* for prod with PgBouncer."
  type        = string
  default     = "B_Standard_B1ms"
}

# -----------------------------------------------------------------
# PgBouncer Configuration (Connection Pooling)
# -----------------------------------------------------------------
variable "pgbouncer_enabled" {
  description = "Enable PgBouncer connection pooling on PostgreSQL Flexible Server."
  type        = bool
  default     = false
}

variable "pgbouncer_default_pool_size" {
  description = "Number of server connections per user/database pair for PgBouncer."
  type        = number
  default     = 50
}

variable "pgbouncer_max_client_conn" {
  description = "Maximum number of client connections allowed by PgBouncer."
  type        = number
  default     = 200
}

variable "pgbouncer_pool_mode" {
  description = "PgBouncer pool mode: 'session', 'transaction', or 'statement'."
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

variable "enable_envoy_gateway" {
  description = "Enable Envoy Gateway for the dev API validation path."
  type        = bool
  default     = true
}

variable "envoy_gateway_namespace" {
  description = "Namespace where the Envoy Gateway controller runs."
  type        = string
  default     = "envoy-gateway-system"
}

variable "envoy_gateway_chart_version" {
  description = "Pinned Envoy Gateway Helm chart version."
  type        = string
  default     = "v1.8.1"
}

variable "envoy_gateway_public_ip_name" {
  description = "Name for the static public IP used by Envoy Gateway."
  type        = string
  default     = "copilot-gtm-envoy-gateway-dev-pip"
}

variable "envoy_gateway_class_name" {
  description = "GatewayClass name used by application Gateway API resources."
  type        = string
  default     = "envoy-gateway-dev"
}

variable "envoy_validation_subdomain" {
  description = "Temporary API subdomain retained until the post-AGIC cleanup apply."
  type        = string
  default     = "devapi-envoy"
}

# KEDA Configuration
variable "keda_namespace" {
  type        = string
  description = "Namespace for KEDA system components"
  default     = "keda-system"
}

variable "vnet_waf_subnet" {
  description = "Legacy Application Gateway subnet CIDR. Kept to avoid VNet churn while deleting the dev gateway."
  type        = string
  default     = "10.0.5.0/24"
}

# -----------------------------------------------------------------
# Microsoft Teams Bot
# -----------------------------------------------------------------

variable "teams_bot_messaging_endpoint" {
  description = <<-DESC
    The HTTPS endpoint where the bot receives messages from Teams.

    For local development: Use ngrok URL (e.g., https://xxx.ngrok-free.app/api/teams/messages)
    For production: Use your actual API endpoint (e.g., https://api.copilotgtm.com/api/teams/messages)

    This can be updated later via Azure Portal or by re-running Terraform.
  DESC
  type        = string
  default     = "https://devapi.copilotgtm.com/users/bots/teams/messages"
}

# -----------------------------------------------------------------
# OAuth App Registration
# -----------------------------------------------------------------
variable "oauth_redirect_uris" {
  description = "OAuth callback redirect URIs for Microsoft app registration"
  type        = list(string)
  default     = []
}

# -----------------------------------------------------------------
# Demo Frontend App Service
# -----------------------------------------------------------------

variable "demo_frontend_app_service_plan_name" {
  description = "The name of the App Service Plan for the demo frontend app."
  type        = string
  default     = "copilot-gtm-demo-asp"
}

variable "demo_frontend_app_service_name" {
  description = "The name of the demo Frontend App Service."
  type        = string
  default     = "copilot-gtm-demo-app"
}

variable "demo_frontend_docker_image_name" {
  description = "The name of the Docker image for the demo frontend app."
  type        = string
  default     = "copilotgtm-frontend-app"
}

variable "demo_frontend_docker_image_tag" {
  description = "The tag of the Docker image for the demo frontend app."
  type        = string
  default     = "latest"
}

variable "demo_frontend_sku_name" {
  description = "The SKU of the App Service Plan for the demo frontend. Use B1 for the low-cost plan."
  type        = string
  default     = "B1"
}

variable "demo_frontend_os_type" {
  description = "The OS type of the App Service Plan for the demo frontend."
  type        = string
  default     = "Linux"
}

variable "demo_frontend_https_only" {
  description = "Whether to enable HTTPS only on the App Service for the demo frontend."
  type        = bool
  default     = true
}

# -----------------------------------------------------------------
# Demo Frontend Pipeline
# -----------------------------------------------------------------

variable "demo_pipeline_name" {
  description = "Name of the Azure DevOps pipeline resource for demo frontend deployments."
  type        = string
  default     = "demo pipeline"
}

variable "demo_pipeline_yml_path" {
  description = "Relative path to the dedicated manual-only YAML definition used by the demo frontend pipeline."
  type        = string
  default     = "pipelines/.azure-pipelines/azure-pipelines-demo.yml"
}

variable "demo_pipeline_branch" {
  description = "Default branch for the manual-only demo pipeline definition."
  type        = string
  default     = "demo/stakeholder-map"
}

# -----------------------------------------------------------------
# Demo Frontend DNS
# -----------------------------------------------------------------

variable "demo_frontend_dns_a_record_name" {
  description = "The name of the DNS record for the demo frontend app service."
  type        = string
  default     = "demoapp"
}

variable "demo_frontend_dns_zone_name" {
  description = "DNS zone for the demo frontend hostname."
  type        = string
  default     = "copilotgtm.com"
}

variable "demo_frontend_dns_zone_resource_group" {
  description = "Optional resource group override for the demo frontend DNS zone. Defaults to dns_zone_resource_group when null."
  type        = string
  default     = null
}




