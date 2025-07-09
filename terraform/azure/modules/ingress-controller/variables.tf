variable "resource_group_name" {
  description = "Name of the resource group to deploy into"
  type        = string
}

variable "location" {
  description = "Azure region for resources"
  type        = string
}

variable "namespace" {
  description = "Kubernetes namespace for the ingress controller"
  type        = string
  default     = "nginx-ingress"
}

variable "release_name" {
  description = "Helm release name for the ingress controller"
  type        = string
}

variable "chart_version" {
  description = "Version of the ingress-nginx Helm chart to install"
  type        = string
  default     = "4.13.0"
}

variable "values_file_path" {
  description = "Path to your custom values.yaml"
  type        = string
}