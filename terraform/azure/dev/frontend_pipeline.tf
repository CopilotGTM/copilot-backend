
# -----------------------------------------------------------------
# Azure DevOps Pipeline (CI/CD) for Frontend
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
}
