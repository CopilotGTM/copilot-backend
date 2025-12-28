# -----------------------------------------------------------------
# Common Variables
# -----------------------------------------------------------------
env                 = "dev"
subscription_id     = "675df0ed-8e06-441b-a1e4-110d19521816"
resource_group_name = "copilot-gtm-app"
location            = "centralus"
project_name        = "copilotgtm"
db_location         = "centralus"
db_name             = "copilotgtm-postgresdb-centralus-dev"

# -----------------------------------------------------------------
# Networking
# -----------------------------------------------------------------
vnet_name          = "copilot-gtm-vnet"
vnet_address_space = ["10.0.0.0/16"]
vnet_public_subnets = ["10.0.1.0/24"]

# Two private subnets: one for AKS/microservices, one for Redis/data
vnet_private_subnets = [
  "10.0.2.0/24", # private-aks subnet
  "10.0.3.0/24"  # private-data (Redis, DB) subnet
]

# Dedicated subnet for Private Link private endpoints
vnet_private_endpoints_subnet = "10.0.4.0/24"

# Private Link (mandatory for Key Vault module)
enable_key_vault_private_endpoints = true

# Private Link (Storage)
enable_storage_private_endpoints = true

# -----------------------------------------------------------------
# Azure Kubernetes Service (AKS)
# -----------------------------------------------------------------
aks_name           = "copilot-gtm-aks"
aks_dns_prefix     = "aks"
aks_node_count     = 2
aks_vm_size        = "Standard_D4s_v3"
aks_service_cidr   = "10.1.0.0/16"
aks_dns_service_ip = "10.1.0.10"
aks_os_disk_size_gb = 128
aks_main_app_namespace = "copilotgtm"
aks_main_app_service_account = "copilot-backend"

# -----------------------------------------------------------------
# Azure Container Registry (ACR)
# -----------------------------------------------------------------
acr_name = "copilotgtmacr"
acr_type = "Premium"

# -----------------------------------------------------------------
# Azure Redis Cache
# -----------------------------------------------------------------
redis_name = "copilot-gtm-redis"
redis_sku_name = "Basic"
redis_capacity = 0
redis_family = "C"

# -----------------------------------------------------------------
# Frontend App Service
# -----------------------------------------------------------------
frontend_app_service_plan_name = "copilot-gtm-appservice-frontend-asp"
frontend_app_service_name      = "copilot-gtm-frontend-appservice"
frontend_docker_image_name     = "copilotgtm-frontend-app"
frontend_docker_image_tag      = "latest"
frontend_sku_name = "B2"
frontend_os_type = "Linux"
frontend_https_only = true

# -----------------------------------------------------------------
# Frontend Admin App Service
# -----------------------------------------------------------------
frontend_admin_app_service_plan_name = "copilot-gtm-admin-appservice-frontend-asp"
frontend_admin_app_service_name      = "copilot-gtm-admin-frontend-appservice"
frontend_admin_docker_image_name     = "copilotgtm-frontend-admin-app"
frontend_admin_docker_image_tag      = "latest"
frontend_admin_sku_name = "B1"
frontend_admin_os_type = "Linux"
frontend_admin_https_only = true

# -----------------------------------------------------------------
# Storage Account
# -----------------------------------------------------------------
storage_account_name = "masterstorage"

# -----------------------------------------------------------------
# Frontend Service
# -----------------------------------------------------------------
frontend_repo_name = "copilot-app-frontend"
frontend_pipeline_name = "copilot-app-frontend"
frontend_yml_path = "pipelines/.azure-pipelines/azure-pipelines.yml"
frontend_dns_a_record_name = "devapp"
dns_zone_name       = "copilotgtm.com"
dns_zone_resource_group       = "master-resources"
frontend_branch = "dev"

# -----------------------------------------------------------------
# Frontend Admin Service
# -----------------------------------------------------------------
frontend_admin_repo_name = "copilot-admin-frontend"
frontend_admin_pipeline_name = "copilot-admin-frontend"
frontend_admin_yml_path = "pipelines/.azure-pipelines/azure-pipelines.yml"
frontend_admin_dns_a_record_name = "devadmin"
frontend_admin_branch = "dev"

# -----------------------------------------------------------------
# Azure DevOps
# -----------------------------------------------------------------
# Connection & Project
org_service_url     = "https://dev.azure.com/copilotgtm"
devops_project_name = "app-copilotgtm"
devops_environment_name = "development"

# Service Principal for Azure DevOps
sp_name            = "copilot-gtm-devops-sp"

# -----------------------------------------------------------------
# Repository & Pipeline
# -----------------------------------------------------------------
backend_repo_name     = "copilot-backend"
backend_pipeline_name = "backend-pipeline"
backend_yml_path      = ".azure-pipelines/azure-pipelines.yml"
backend_branch = "dev"

# -----------------------------------------------------------------
# Authentication
# -----------------------------------------------------------------
# azuredevops_pat is provided via Azure DevOps Variable Group (TF_VAR_azuredevops_pat)
azuredevops_sp_display_name = "TerraformSP"


# -----------------------------------------------------------------
# Static DevOps Agent VM
# -----------------------------------------------------------------
admin_username = "azureuser"
# admin_password is provided via Azure DevOps Variable Group (TF_VAR_admin_password)
agent_pool     = "StaticAgentPool"
agent_name     = "gtm-devops-agent-01"

# AZURE OPENAI
# azure_openai_key is provided via Azure DevOps Variable Group (TF_VAR_azure_openai_key)

# Cert-Manager Configuration
letsencrypt_email = "nikhil@copilotgtm.com"

# -----------------------------------------------------------------
# K8s Addons Configuration (Migrated)
# -----------------------------------------------------------------
# letsencrypt_email is already defined above
azure_dns_managed_identity_client_id = "d999da04-d31f-43f5-b27d-2a6ddce80e13"
api_subdomain                        = "devapi"
app_namespace                        = "copilotgtm"

# KEDA
# enable_keda_autoscaling is already defined above
keda_namespace          = "keda-system"

# -----------------------------------------------------------------
# Your specific certificates
# -----------------------------------------------------------------
certificates = {
  "api-copilotgtm-com" = {
    name        = "api-copilotgtm-com"
    namespace   = "copilot"
    secret_name = "api-copilotgtm-com-tls"
    issuer_name = "letsencrypt-prod"
    issuer_kind = "ClusterIssuer"
    common_name = "devapi.copilotgtm.com"
    dns_names   = ["devapi.copilotgtm.com"]
  }
}

# -----------------------------------------------------------------
# Supabase Variables
# -----------------------------------------------------------------
super_admin_email = "nikhil@copilotgtm.com"
supabase_url = "https://mdpbmdnwwdkjcxefbbbk.supabase.co"
# supabase_anon_key is provided via Azure DevOps Variable Group (TF_VAR_supabase_anon_key)
# supabase_service_role_key is provided via Azure DevOps Variable Group (TF_VAR_supabase_service_role_key)
# supabase_jwt_secret is provided via Azure DevOps Variable Group (TF_VAR_supabase_jwt_secret)

# -----------------------------------------------------------------
# Resend API Key
# -----------------------------------------------------------------
# resend_api_key is provided via Azure DevOps Variable Group (TF_VAR_resend_api_key)
resend_email = "noreply@team.copilotgtm.com"

# -----------------------------------------------------------------
# KEDA Configuration
# -----------------------------------------------------------------
enable_keda_autoscaling = true
keda_polling_interval = 30
keda_cooldown_period = 300

# -----------------------------------------------------------------
# GPU Knowledge Worker Node Pool
# -----------------------------------------------------------------
# High-Performance Compute Node Pool (Knowledge Worker)
# Using latest-gen AMD EPYC Genoa (4th Gen) for best x86_64 performance
# D8as_v6: AMD EPYC Genoa @ up to 3.7GHz, 32GB RAM (4:1 memory ratio)
# ~$0.37/hr - 31% cheaper than D8s_v3, works with existing x86_64 Docker images
# Ideal for compute-intensive AI/ML workloads with higher memory requirements
enable_gpu_worker_pool = true
gpu_node_pool_name = "kgpu"
gpu_vm_size = "Standard_D8s_v3"
gpu_min_count = 0  # Scale to zero when no tasks
gpu_max_count = 1  # Single node for knowledge worker  
gpu_node_labels = {
  workload = "knowledge-gpu"
}
gpu_node_taints = ["dedicated=gpu:NoSchedule"]

# Nango Config
# nango_secret is provided via Azure DevOps Variable Group (TF_VAR_nango_secret)

# -----------------------------------------------------------------
# Terraform Pipeline
# -----------------------------------------------------------------
terraform_repo_name     = "copilot-app-infra"
terraform_pipeline_name = "copilot-app-infra-pipeline"
terraform_yml_path      = ".pipeline/.azure-pipeline.yml"


