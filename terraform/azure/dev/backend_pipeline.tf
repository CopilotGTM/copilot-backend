# -----------------------------------------------------------------
# Service Principal for Azure DevOps
# -----------------------------------------------------------------
module "service_principal" {
  source          = "../modules/service_principal"
  sp_name         = var.sp_name
  subscription_id = var.subscription_id
}

# -----------------------------------------------------------------
# Azure DevOps Service Connection (for ACR/AKS)
# -----------------------------------------------------------------
data "azuredevops_project" "project" {
  name = var.devops_project_name
}

module "service_connection" {
  source                   = "../modules/service_connection"
  devops_project_id        = data.azuredevops_project.project.id
  service_connection_name  = "copilotgtm-arc-ci-cd-service--${var.env}"
  subscription_id          = var.subscription_id
  subscription_name        = "Copilot GTM Subscription"
  tenant_id                = module.service_principal.tenant_id
  client_id                = module.service_principal.client_id
  client_secret            = module.service_principal.client_secret
}

# -----------------------------------------------------------------
# Azure DevOps Pipeline (CI/CD)
# -----------------------------------------------------------------
data "azuredevops_git_repository" "repo" {
  project_id = data.azuredevops_project.project.id
  name       = var.backend_repo_name
}

data "azuredevops_variable_group" "secrets" {
  project_id = data.azuredevops_project.project.id
  name       = "app-secrets-${var.env}"
  depends_on = [
    module.service_connection
  ]
}

module "pipeline" {
  source             = "../modules/pipeline"
  devops_project_id  = data.azuredevops_project.project.id
  pipeline_name      = "${var.backend_pipeline_name}-${var.env}"
  repo_id            = data.azuredevops_git_repository.repo.id
  branch_name        = "main"
  yml_path           = var.backend_yml_path
  variable_group_ids = [data.azuredevops_variable_group.secrets.id]
}

# -----------------------------------------------------------------
# Azure DevOps Service Connection for ACR (Docker Registry)
# -----------------------------------------------------------------
module "service_connection_acr" {
  source                = "../modules/service_connection_acr"
  devops_project_id     = data.azuredevops_project.project.id
  service_endpoint_name = "copilotgtm-acr-connection-${var.env}"
  acr_login_server      = module.acr.acr_login_server
  acr_sp_app_id         = module.service_principal.client_id
  acr_sp_app_secret     = module.service_principal.client_secret
}

# -----------------------------------------------------------------
# Azure DevOps Secure File for Kubeconfig
# -----------------------------------------------------------------
module "secure_file" {
  source              = "../modules/secure_file"
  devops_project_id   = data.azuredevops_project.project.id
  secure_file_name    = "kubeconfig.yaml"
  secure_file_content = module.aks.kube_config
  apiserver_url       = module.aks.host
  cluster_context     = module.aks.aks_name
  depends_on = [
    module.aks
  ]
}
