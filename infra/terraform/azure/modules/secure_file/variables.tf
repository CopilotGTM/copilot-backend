variable "devops_project_id" {
  description = "The ID of the Azure DevOps project."
  type        = string
}

variable "secure_file_name" {
  description = "The name of the secure file."
  type        = string
}

variable "secure_file_content" {
  description = "The content of the secure file."
  type        = string
  sensitive   = true
}

variable "apiserver_url" {
  description = "The URL of the Kubernetes API server."
  type        = string
}

variable "cluster_context" {
  description = "The context for the Kubernetes cluster."
  type        = string
}
