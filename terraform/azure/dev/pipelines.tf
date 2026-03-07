# =================================================================
# Infrastructure Pipeline
# =================================================================

# -----------------------------------------------------------------
# Azure DevOps Agent Pool & Queue
# -----------------------------------------------------------------
resource "azuredevops_agent_pool" "pool" {
  name           = var.agent_pool
  auto_provision = false
  pool_type      = "automation"
}

resource "azuredevops_agent_queue" "queue" {
  project_id    = data.azuredevops_project.project.id
  agent_pool_id = azuredevops_agent_pool.pool.id
}

resource "azuredevops_pipeline_authorization" "queue_auth" {
  project_id  = data.azuredevops_project.project.id
  resource_id = azuredevops_agent_queue.queue.id
  type        = "queue"
}

# -----------------------------------------------------------------
# Azure DevOps Variable Group (Secrets)
# -----------------------------------------------------------------
# NOTE: TF_VAR_azuredevops_pat must be added MANUALLY to this variable group
# after Terraform creates it. This is intentional - the PAT is a sensitive
# secret that should be managed by the user, not stored in Terraform state.
#
# After bootstrap, go to:
#   Azure DevOps → Pipelines → Library → copilot-infra-secrets-<env>
#   Add variable: TF_VAR_azuredevops_pat (mark as secret)
# -----------------------------------------------------------------
resource "azuredevops_variable_group" "infra_secrets" {
  project_id   = data.azuredevops_project.project.id
  name         = "${var.infra_variable_group_name}-${var.env}"
  description  = "Managed by Terraform. Add TF_VAR_azuredevops_pat manually after creation."
  allow_access = true

  variable {
    name  = "MANAGED_BY"
    value = "terraform"
  }

  # Prevent subsequent Terraform runs from modifying manually added variables
  # This ensures TF_VAR_azuredevops_pat (added manually) is preserved
  lifecycle {
    ignore_changes = [variable]
  }
}

# -----------------------------------------------------------------
# Terraform Pipeline (DEV Environment)
# -----------------------------------------------------------------
# Uses dev-specific pipeline YAML: .pipeline/azure-pipeline-dev.yml
# Triggered on: main, feature/*, hotfix/* branches
# -----------------------------------------------------------------
data "azuredevops_git_repository" "terraform_repo" {
  project_id = data.azuredevops_project.project.id
  name       = var.terraform_repo_name
}

module "infra_pipeline" {
  source             = "../modules/pipeline"
  devops_project_id  = data.azuredevops_project.project.id
  pipeline_name      = "${var.terraform_pipeline_name}-${var.env}"
  repo_id            = data.azuredevops_git_repository.terraform_repo.id
  branch_name        = var.terraform_branch
  yml_path           = var.terraform_yml_path
  variable_group_ids = [azuredevops_variable_group.infra_secrets.id]

  pipeline_variables = {}
}

# =================================================================
# Backend Pipeline (Shared - handles both Dev and Prod)
# =================================================================
# This single pipeline handles all environments using branch conditions.
# -----------------------------------------------------------------

data "azuredevops_git_repository" "repo" {
  project_id = data.azuredevops_project.project.id
  name       = var.backend_repo_name
}

module "pipeline" {
  source             = "../modules/pipeline"
  devops_project_id  = data.azuredevops_project.project.id
  pipeline_name      = "${var.backend_pipeline_name}-${var.env}"
  repo_id            = data.azuredevops_git_repository.repo.id
  branch_name        = var.backend_branch
  yml_path           = var.backend_yml_path
  variable_group_ids = [azuredevops_variable_group.secrets.id]
  pipeline_variables = {
    # Pipeline variables are set per stage in the YAML
  }
}

# -----------------------------------------------------------------
# Azure DevOps Secure File for Kubeconfig
# -----------------------------------------------------------------

module "secure_file" {
  source              = "../modules/secure_file"
  devops_project_id   = data.azuredevops_project.project.id
  secure_file_name    = "kubeconfig-dev.yaml"
  secure_file_content = module.aks.kube_config
  apiserver_url       = module.aks.host
  cluster_context     = module.aks.aks_name
  depends_on = [
    module.aks
  ]
}

# =================================================================
# Frontend App & Pipeline
# =================================================================

# -----------------------------------------------------------------
# App Service: Azure Web App for frontend hosting (Node.js)
# -----------------------------------------------------------------

module "frontend_app_service" {
  source                = "../modules/app_service"
  app_service_plan_name = "${var.frontend_app_service_plan_name}-${var.env}"
  app_service_name      = "${var.frontend_app_service_name}-${var.env}"
  resource_group_name   = module.resource_group.resource_group_name
  location              = var.location
  docker_image_name     = var.frontend_docker_image_name
  docker_image_tag      = var.frontend_docker_image_tag
  docker_registry_url   = module.acr.acr_login_server
  acr_id                = module.acr.acr_id
  app_settings          = {}
  sku_name              = var.frontend_sku_name
  os_type               = var.frontend_os_type
  https_only            = var.frontend_https_only

  # Custom domain configuration
  custom_domain_name      = "${var.frontend_dns_a_record_name}.${var.dns_zone_name}"
  dns_zone_name           = var.dns_zone_name
  dns_zone_resource_group = var.dns_zone_resource_group
  create_dns_records      = true

  depends_on = [
    module.resource_group,
    module.acr
  ]
}

# -----------------------------------------------------------------
# Azure DevOps Pipeline (CI/CD) for Frontend (Shared - handles both Dev and Prod)
# -----------------------------------------------------------------

data "azuredevops_git_repository" "frontend_repo" {
  project_id = data.azuredevops_project.project.id
  name       = var.frontend_repo_name
}

module "frontend_pipeline" {
  source             = "../modules/pipeline"
  devops_project_id  = data.azuredevops_project.project.id
  pipeline_name      = "${var.frontend_pipeline_name}-${var.env}"
  repo_id            = data.azuredevops_git_repository.frontend_repo.id
  branch_name        = var.frontend_branch
  yml_path           = var.frontend_yml_path
  variable_group_ids = [azuredevops_variable_group.secrets.id]
  pipeline_variables = {
    # Pipeline variables are set per stage in the YAML
  }
}

# =================================================================
# Frontend Admin App & Pipeline
# =================================================================

# -----------------------------------------------------------------
# Admin App Service: Azure Web App for frontend hosting
# -----------------------------------------------------------------

module "frontend_admin_app_service" {
  source                = "../modules/app_service"
  app_service_plan_name = "${var.frontend_admin_app_service_plan_name}-${var.env}"
  app_service_name      = "${var.frontend_admin_app_service_name}-${var.env}"
  resource_group_name   = module.resource_group.resource_group_name
  location              = var.location
  docker_image_name     = var.frontend_admin_docker_image_name
  docker_image_tag      = var.frontend_admin_docker_image_tag
  docker_registry_url   = module.acr.acr_login_server
  acr_id                = module.acr.acr_id
  app_settings          = {}
  sku_name              = var.frontend_admin_sku_name
  os_type               = var.frontend_admin_os_type
  https_only            = var.frontend_admin_https_only

  # Custom domain configuration
  custom_domain_name      = "${var.frontend_admin_dns_a_record_name}.${var.dns_zone_name}"
  dns_zone_name           = var.dns_zone_name
  dns_zone_resource_group = var.dns_zone_resource_group
  create_dns_records      = true

  depends_on = [
    module.resource_group,
    module.acr
  ]
}

# -----------------------------------------------------------------
# Azure DevOps Pipeline (CI/CD) for Admin Frontend (Shared - handles both Dev and Prod)
# -----------------------------------------------------------------

data "azuredevops_git_repository" "frontend_admin_repo" {
  project_id = data.azuredevops_project.project.id
  name       = var.frontend_admin_repo_name
}

module "frontend_admin_pipeline" {
  source             = "../modules/pipeline"
  devops_project_id  = data.azuredevops_project.project.id
  pipeline_name      = "${var.frontend_admin_pipeline_name}-${var.env}"
  repo_id            = data.azuredevops_git_repository.frontend_admin_repo.id
  branch_name        = var.frontend_admin_branch
  yml_path           = var.frontend_admin_yml_path
  variable_group_ids = [azuredevops_variable_group.secrets.id]
  pipeline_variables = {
    # Pipeline variables are set per stage in the YAML
  }
}

# =================================================================
# Demo Frontend App & Pipeline (Dev only)
# =================================================================

module "demo_frontend_app_service" {
  source                = "../modules/app_service"
  app_service_plan_name = "${var.demo_frontend_app_service_plan_name}-${var.env}"
  app_service_name      = "${var.demo_frontend_app_service_name}-${var.env}"
  resource_group_name   = module.resource_group.resource_group_name
  location              = var.location
  docker_image_name     = var.demo_frontend_docker_image_name
  docker_image_tag      = var.demo_frontend_docker_image_tag
  docker_registry_url   = module.acr.acr_login_server
  acr_id                = module.acr.acr_id
  app_settings          = {}
  sku_name              = var.demo_frontend_sku_name
  os_type               = var.demo_frontend_os_type
  https_only            = var.demo_frontend_https_only

  custom_domain_name      = "${var.demo_frontend_dns_a_record_name}.${var.demo_frontend_dns_zone_name}"
  dns_zone_name           = var.demo_frontend_dns_zone_name
  dns_zone_resource_group = coalesce(var.demo_frontend_dns_zone_resource_group, var.dns_zone_resource_group)
  create_dns_records      = true

  depends_on = [
    module.resource_group,
    module.acr
  ]
}

module "demo_pipeline" {
  source             = "../modules/pipeline"
  devops_project_id  = data.azuredevops_project.project.id
  pipeline_name      = var.demo_pipeline_name
  repo_id            = data.azuredevops_git_repository.frontend_repo.id
  branch_name        = var.demo_pipeline_branch
  yml_path           = var.demo_pipeline_yml_path
  variable_group_ids = [azuredevops_variable_group.secrets.id]
  pipeline_variables = {
    APP_URL      = module.demo_frontend_app_service.custom_domain_url
    WEB_APP_NAME = module.demo_frontend_app_service.app_service_name
  }
}

