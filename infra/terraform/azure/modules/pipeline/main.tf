resource "azuredevops_build_definition" "pipeline" {
  project_id = var.devops_project_id
  name       = var.pipeline_name

  repository {
    repo_type   = "TfsGit"
    repo_id     = var.repo_id
    branch_name = startswith(var.branch_name, "refs/heads/") ? var.branch_name : "refs/heads/${var.branch_name}"
    yml_path    = var.yml_path
  }

  ci_trigger {
    use_yaml = true
  }

  variable_groups = var.variable_group_ids

  dynamic "variable" {
    for_each = var.pipeline_variables
    content {
      name  = variable.key
      value = variable.value
    }
  }
}
