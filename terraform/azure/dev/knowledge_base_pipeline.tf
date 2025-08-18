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
