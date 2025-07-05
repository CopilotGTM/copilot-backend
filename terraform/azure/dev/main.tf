module "resource_group" {
  source              = "../modules/resource_group"
  resource_group_name = "${var.resource_group_name}-${var.env}"
  location            = var.location
}

module "vnet" {
  source              = "../modules/vnet"
  vnet_name           = "${var.vnet_name}-${var.env}"
  address_space       = var.vnet_address_space
  location            = module.resource_group.resource_group_location
  resource_group_name = module.resource_group.resource_group_name
  public_subnets      = var.vnet_public_subnets
  private_subnets     = var.vnet_private_subnets
  env                 = var.env
}

module "aks" {
  source               = "../modules/aks"
  aks_name             = "${var.aks_name}-${var.env}"          # Unique per environment
  dns_prefix           = "${var.aks_dns_prefix}-${var.env}"    # Unique per environment
  location             = module.resource_group.resource_group_location
  resource_group_name  = module.resource_group.resource_group_name
  subnet_id            = module.vnet.private_subnet_ids[0]
  agent_count          = var.aks_node_count
  vm_size              = var.aks_vm_size
  service_cidr         = var.aks_service_cidr
  dns_service_ip       = var.aks_dns_service_ip
  env                  = var.env
}

module "acr" {
  source              = "../modules/acr"
  acr_name            = "${var.acr_name}${var.env}"
  resource_group_name = module.resource_group.resource_group_name
  location            = var.location
  sku                 = "Basic"
  admin_enabled       = false
}

resource "azurerm_role_assignment" "aks_kubelet_acr_pull" {
  principal_id         = module.aks.aks_principal_id
  role_definition_name = "AcrPull"
  scope                = module.acr.acr_id
  principal_type       = "ServicePrincipal"
}

module "redis" {
  source              = "../modules/redis"
  redis_name          = "${var.redis_name}-${var.env}"
  resource_group_name = module.resource_group.resource_group_name
  location            = var.location
  capacity            = 0
  family              = "C"
  tags = {
    environment = var.env
    project     = var.project_name
  }
}

module "frontend_app_service" {
  source                  = "../modules/app_service"
  app_service_plan_name   = "${var.frontend_app_service_plan_name}-${var.env}"
  app_service_name        = "${var.frontend_app_service_name}-${var.env}"
  resource_group_name     = module.resource_group.resource_group_name
  location                = var.location
  node_version            = "20-lts"
  app_settings            = {}
}

resource "random_integer" "suffix" {
  min = 10000
  max = 99999
}
module "storage_account" {
  source                  = "../modules/storage_account"
  storage_account_name    = "${var.storage_account_name}${var.env}"
  resource_group_name     = module.resource_group.resource_group_name
  location                = var.location
  account_tier            = "Standard"
  account_replication_type = "LRS"
  tags = {
    environment = var.env
    project     = var.project_name
  }
}

# Azure DevOps resources: data sources for project and repository
data "azuredevops_project" "this" {
  name = var.devops_project_name
}

data "azuredevops_git_repository" "this" {
  project_id = data.azuredevops_project.this.id
  name       = var.backend_repo_name
}

# Pipeline module call: creates the YAML pipeline resource in Azure DevOps
module "devops_pipeline" {
  source        = "../modules/devops_pipeline"
  project_id    = data.azuredevops_project.this.id
  pipeline_name = var.backend_pipeline_name
  repo_id       = data.azuredevops_git_repository.this.id
  branch_name   = "refs/heads/main"             # <-- Or your default branch
  yml_path      = var.backend_yml_path
  # pipeline_path left default (root)
}

# Create Azure DevOps Environment for pipeline deployment targets using tfvars
resource "azuredevops_environment" "pipeline_env" {
  project_id = data.azuredevops_project.this.id
  name       = var.devops_environment_name
  description = "Deployment environment '${var.devops_environment_name}' for Helm/AKS pipeline targets in DevOps project ${var.devops_project_name}"
}

# Role assignment for Azure DevOps pipeline service principal

# Provide your Azure DevOps service principal objectId as a variable or output: var.azuredevops_sp_object_id
# Provide your ACR and AKS resource IDs from module outputs if not already available

resource "azurerm_role_assignment" "pipeline_acr_push" {
  scope                = module.acr.acr_id      # Your ACR resource ID
  role_definition_name = "AcrPush"
  principal_id         = data.azuread_service_principal.devops_pipeline.object_id
}

resource "azurerm_role_assignment" "pipeline_aks_writer" {
  scope                = module.aks.aks_id      # Your AKS resource ID
  role_definition_name = "Azure Kubernetes Service RBAC Writer"
  principal_id         = data.azuread_service_principal.devops_pipeline.object_id
}

# Lookup the Azure DevOps pipeline service principal by display name
data "azuread_service_principal" "devops_pipeline" {
  display_name = var.azuredevops_sp_display_name
}
