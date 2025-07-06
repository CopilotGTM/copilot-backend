# -----------------------------------------------------------------
# Resource Group: Top-level container for all Azure resources
# -----------------------------------------------------------------
module "resource_group" {
  source              = "../modules/resource_group"
  resource_group_name = "${var.resource_group_name}-${var.env}"
  location            = var.location
}

# -----------------------------------------------------------------
# Virtual Network: Provides networking and subnets for AKS and App Services
# -----------------------------------------------------------------
module "vnet" {
  source              = "../modules/vnet"
  vnet_name           = "${var.vnet_name}-${var.env}"
  address_space       = var.vnet_address_space
  location            = module.resource_group.resource_group_location
  resource_group_name = module.resource_group.resource_group_name
  public_subnets      = var.vnet_public_subnets
  private_subnets     = var.vnet_private_subnets
  env                 = var.env
  # private_subnet_dependencies = [module.ephemeral_build_agent]
}

# -----------------------------------------------------------------
# Azure Kubernetes Service: Managed Kubernetes cluster for containers
# -----------------------------------------------------------------
module "aks" {
  source               = "../modules/aks"
  aks_name             = "${var.aks_name}-${var.env}"
  dns_prefix           = "${var.aks_dns_prefix}-${var.env}"
  location             = module.resource_group.resource_group_location
  resource_group_name  = module.resource_group.resource_group_name
  subnet_id            = module.vnet.private_subnet_ids[0]
  agent_count          = var.aks_node_count
  vm_size              = var.aks_vm_size
  os_disk_size_gb      = var.aks_os_disk_size_gb
  service_cidr         = var.aks_service_cidr
  dns_service_ip       = var.aks_dns_service_ip
  env                  = var.env
}

# -----------------------------------------------------------------
# Azure Container Registry: Stores container images for AKS and DevOps pipelines
# -----------------------------------------------------------------
module "acr" {
  source              = "../modules/acr"
  acr_name            = "${var.acr_name}${var.env}"
  resource_group_name = module.resource_group.resource_group_name
  location            = var.location
  sku                 = var.acr_type
}

# -----------------------------------------------------------------
# Role Assignment: Allow AKS kubelet to pull images from ACR
# -----------------------------------------------------------------
resource "azurerm_role_assignment" "aks_kubelet_acr_pull" {
  principal_id         = module.aks.aks_principal_id
  role_definition_name = "AcrPull"
  scope                = module.acr.acr_id
  principal_type       = "ServicePrincipal"
}

# -----------------------------------------------------------------
# Azure Redis Cache: In-memory data store for caching and sessions
# -----------------------------------------------------------------
module "redis" {
  source              = "../modules/redis"
  redis_name          = "${var.redis_name}-${var.env}"
  resource_group_name = module.resource_group.resource_group_name
  location            = var.location
  capacity            = var.redis_capacity
  family              = var.redis_family
  sku_name            = var.redis_sku_name
  tags = {
    environment = var.env
    project     = var.project_name
  }
}

# -----------------------------------------------------------------
# App Service: Azure Web App for frontend hosting (Node.js)
# -----------------------------------------------------------------
module "frontend_app_service" {
  source                  = "../modules/app_service"
  app_service_plan_name   = "${var.frontend_app_service_plan_name}-${var.env}"
  app_service_name        = "${var.frontend_app_service_name}-${var.env}"
  resource_group_name     = module.resource_group.resource_group_name
  location                = var.location
  node_version            = var.frontend_node_version
  app_settings            = {}
}

# # -----------------------------------------------------------------
# # Azure Storage Account: General purpose blob/file storage
# # -----------------------------------------------------------------
# module "storage_account" {
#   source                  = "../modules/storage_account"
#   storage_account_name    = "${var.storage_account_name}${var.env}"
#   resource_group_name     = module.resource_group.resource_group_name
#   location                = var.location
#   account_tier            = "Standard"
#   account_replication_type = "LRS"
#   tags = {
#     environment = var.env
#     project     = var.project_name
#   }
# }

# // -----------------------------------------------------------------
# // Azure: Retrieve current subscription and client configuration
# // -----------------------------------------------------------------
# data "azurerm_client_config" "current" {}
# data "azurerm_subscription" "current" {}

# // -----------------------------------------------------------------
# // Azure DevOps: Retrieve project and repo info for pipelines and auth
# // -----------------------------------------------------------------
# # Azure DevOps Project for Backend
# data "azuredevops_project" "this" {
#   name = var.devops_project_name
# }

# # Azure DevOps Git Repository for Backend
# data "azuredevops_git_repository" "this" {
#   project_id = data.azuredevops_project.this.id
#   name       = var.backend_repo_name
# }

# // -----------------------------------------------------------------
# // Azure DevOps: Pipeline module for YAML pipeline definition
# // -----------------------------------------------------------------
# module "devops_pipeline" {
#   source        = "../modules/devops_pipeline"
#   project_id    = data.azuredevops_project.this.id
#   pipeline_name = var.backend_pipeline_name
#   repo_id       = data.azuredevops_git_repository.this.id
#   branch_name   = "refs/heads/main"
#   yml_path      = var.backend_yml_path
#   # pipeline_path left default (root)
# }

# // -----------------------------------------------------------------
# // Azure DevOps: Create Environment for pipeline deployment targets
# // -----------------------------------------------------------------
# resource "azuredevops_environment" "pipeline_env" {
#   project_id = data.azuredevops_project.this.id
#   name       = var.devops_environment_name
#   description = "Deployment environment '${var.devops_environment_name}' for Helm/AKS pipeline targets in DevOps project ${var.devops_project_name}"
# }

# // -----------------------------------------------------------------
# // Azure DevOps: Service connection to ACR for Docker builds/pushes
# // -----------------------------------------------------------------
# resource "azuredevops_serviceendpoint_dockerregistry" "acr" {
#   project_id            = data.azuredevops_project.this.id
#   service_endpoint_name = "appcopilotgtmdev" # must match your pipeline variable
#   docker_registry       = module.acr.acr_login_server
#   docker_username         = "Others"

#   depends_on = [module.acr]
# }

# // -----------------------------------------------------------------
# // Azure DevOps: Authorize pipeline to use ACR service connection
# // -----------------------------------------------------------------
# resource "azuredevops_pipeline_authorization" "acr_auth" {
#   project_id  = data.azuredevops_project.this.id
#   pipeline_id = module.devops_pipeline.pipeline_id
#   resource_id = azuredevops_serviceendpoint_dockerregistry.acr.id
#   type        = "endpoint"
# }

# # Role assignment for Azure DevOps pipeline service principal
# resource "azurerm_role_assignment" "pipeline_acr_push" {
#   scope                = module.acr.acr_id      # Your ACR resource ID
#   role_definition_name = "AcrPush"
#   principal_id         = data.azuread_service_principal.devops_pipeline.object_id
# }

# resource "azurerm_role_assignment" "pipeline_aks_writer" {
#   scope                = module.aks.aks_id      # Your AKS resource ID
#   role_definition_name = "Azure Kubernetes Service RBAC Writer"
#   principal_id         = data.azuread_service_principal.devops_pipeline.object_id
# }

# // -----------------------------------------------------------------
# // Azure AD: Lookup DevOps pipeline service principal by display name
# // -----------------------------------------------------------------
# data "azuread_service_principal" "devops_pipeline" {
#   display_name = var.azuredevops_sp_display_name
# }

# data "azuread_application" "devops_pipeline" {
#   display_name = var.azuredevops_sp_display_name
# }


# # -----------------------------------------------------------------
# # Azure DevOps Ephemeral Agent (VMSS)
# # -----------------------------------------------------------------
# module "ephemeral_build_agent" {
#   source                = "../modules/ephemeral_build_agent"
#   vmss_name             = "ephemeral-agent-vmss"
#   resource_group_name   = module.resource_group.resource_group_name
#   location              = module.resource_group.resource_group_location
#   subnet_id             = module.vnet.private_subnet_ids[0]
#   admin_password        = var.admin_password
#   org_service_url       = var.org_service_url
#   azuredevops_pat       = var.azuredevops_pat
#   devops_project_name   = var.devops_project_name
#   agent_pool_name       = "EphemeralAgentPool"
#   agent_name_prefix     = "ephemeral-agent"
#   project_tag           = var.project_name
#   vm_sku                = "Standard_D2s_v4"
#   min_instances         = 1
# }

# # -----------------------------------------------------------------
# # Authorize pipeline to use the new Ephemeral Agent Pool
# # -----------------------------------------------------------------
# resource "azuredevops_pipeline_authorization" "ephemeral_pool_auth" {
#   project_id  = data.azuredevops_project.this.id
#   pipeline_id = module.devops_pipeline.pipeline_id
#   resource_id = module.ephemeral_build_agent.vmss_agent_queue_id
#   type        = "queue"
# }
