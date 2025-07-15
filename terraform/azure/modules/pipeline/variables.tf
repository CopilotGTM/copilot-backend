variable "devops_project_id" {
  description = "The Azure DevOps Project ID."
  type        = string
}

variable "pipeline_name" {
  description = "Name for the Azure DevOps build pipeline."
  type        = string
}

variable "repo_id" {
  description = "The ID of the Azure Repos git repository. Typically: project_name/repo_name."
  type        = string
}

variable "branch_name" {
  description = "Default branch name (e.g., 'main')."
  type        = string
}

variable "yml_path" {
  description = "Path to the azure-pipelines.yml definition in the repository."
  type        = string
}

variable "variable_group_ids" {
  description = "A list of variable group IDs to associate with the pipeline."
  type        = list(number)
  default     = []
}
