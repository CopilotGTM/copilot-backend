# -----------------------------------------------------------------
# Azure DevOps Pipeline (CI/CD)
# -----------------------------------------------------------------
data "azuredevops_git_repository" "knowledge_base_repo" {
  project_id = data.azuredevops_project.project.id
  name       = var.knowledge_base_repo_name
}

module "knowledge_base_pipeline" {
  source             = "../modules/pipeline"
  devops_project_id  = data.azuredevops_project.project.id
  pipeline_name      = "${var.knowledge_base_pipeline_name}-${var.env}"
  repo_id            = data.azuredevops_git_repository.knowledge_base_repo.id
  branch_name        = var.knowledge_base_branch
  yml_path           = var.knowledge_base_yml_path
  variable_group_ids = [azuredevops_variable_group.secrets.id]
  pipeline_variables = {
    DEPLOY_ENVIRONMENT     = var.env
    ACR_LOGIN_SERVER       = module.acr.acr_login_server
    HELM_RELEASE_NAME      = "knowledge-base-worker"
    ACR_SERVICE_CONNECTION = module.service_connection_acr.acr_service_connection_name
  }
}

# -----------------------------------------------------------------
# Azure DevOps Secure File for Kubeconfig
# -----------------------------------------------------------------
module "knowledge_base_secure_file" {
  source              = "../modules/secure_file"
  devops_project_id   = data.azuredevops_project.project.id
  secure_file_name    = "knowledge-base-kubeconfig.yaml"
  secure_file_content = module.aks.kube_config
  apiserver_url       = module.aks.host
  cluster_context     = module.aks.aks_name
  depends_on = [
    module.aks
  ]
}
