# -----------------------------------------------------------------
# Admin App Service: Azure Web App for frontend hosting
# -----------------------------------------------------------------
module "frontend_admin_app_service" {
  source                  = "../modules/app_service"
  app_service_plan_name   = "${var.frontend_admin_app_service_plan_name}-${var.env}"
  app_service_name        = "${var.frontend_admin_app_service_name}-${var.env}"
  resource_group_name     = module.resource_group.resource_group_name
  location                = var.location
  docker_image_name       = var.frontend_admin_docker_image_name
  docker_registry_url     = module.acr.acr_login_server
  acr_id                  = module.acr.acr_id
  app_settings            = {}
  sku_name                = var.frontend_admin_sku_name
  os_type                 = var.frontend_admin_os_type
  https_only              = var.frontend_admin_https_only
  
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
# Azure DevOps Pipeline (CI/CD) for Admin Frontend
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
}