# Module: devops_pipeline
# Purpose: Creates an Azure DevOps YAML pipeline resource, linking to azure-pipelines.yml in a Git repo.

resource "azuredevops_build_definition" "this" {
  project_id     = var.project_id
  name           = var.pipeline_name
  path           = var.pipeline_path
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
