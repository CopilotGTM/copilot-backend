
# -----------------------------------------------------------------
# Azure DevOps Pipeline (CI/CD)
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
    DEPLOY_ENVIRONMENT     = var.env
    ACR_LOGIN_SERVER       = module.acr.acr_login_server
    HELM_RELEASE_NAME      = "copilot-backend"
    ACR_SERVICE_CONNECTION = module.service_connection_acr.acr_service_connection_name
  }
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
