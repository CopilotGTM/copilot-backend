# Variables for devops_pipeline module

variable "project_id" {
  description = "Azure DevOps Project ID. Obtain via data source or output."
  type        = string
}

variable "pipeline_name" {
  description = "Name for the Azure DevOps pipeline resource (arbitrary but unique within project)."
  type        = string
}

variable "pipeline_path" {
  description = "Path/folder in Azure DevOps Pipeline tree (usually '\\' for root)."
  type        = string
  default     = "\\"
}

variable "repo_id" {
  description = "Repository ID for the source git repo (data source or output)."
  type        = string
}

variable "branch_name" {
  description = "Branch ref to connect to pipeline, e.g. 'refs/heads/main'."
  type        = string
  default     = "refs/heads/main"
}

variable "yml_path" {
  description = "Path to the YAML pipeline definition in the repository."
  type        = string
  default     = "azure-pipelines.yml"
}