variable "release_name" {
  description = "Name for the Helm release"
  type        = string
  default     = "nginx-ingress"
}

variable "namespace" {
  description = "Namespace to deploy nginx ingress controller"
  type        = string
  default     = "ingress-nginx"
}

variable "chart_version" {
  description = "Chart version for nginx ingress controller"
  type        = string
  default     = "4.10.1"
}

variable "service_type" {
  description = "nginx Ingress service type (LoadBalancer or ClusterIP)"
  type        = string
  default     = "LoadBalancer"
}

variable "replica_count" {
  description = "Number of nginx ingress controller replicas"
  type        = number
  default     = 1
}

variable "aks_dependency" {
  description = "Resource dependency for AKS cluster (pass module.aks)"
  type        = any
}

variable "resource_group_name" {
  description = "The name of the resource group"
  type        = string
}

variable "location" {
  description = "The Azure location where the resources will be created"
  type        = string
}

variable "tags" {
  description = "A map of tags to assign to the resources"
  type        = map(string)
  default     = {}
}