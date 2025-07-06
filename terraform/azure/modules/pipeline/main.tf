resource "azuredevops_build_definition" "pipeline" {
  project_id = var.devops_project_id
  name       = var.pipeline_name

  repository {
    repo_type   = "TfsGit"
    repo_id     = var.repo_id
    branch_name = var.branch_name
    yml_path    = var.yml_path
  }

  ci_trigger {
    use_yaml = true
  }
}