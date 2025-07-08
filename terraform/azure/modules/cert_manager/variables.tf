variable "aks_dependency" {
  description = "Dependency on the AKS module to ensure cert-manager is installed after AKS."
  type        = any
}

variable "ingress_controller_dependency" {
  description = "Dependency on the ingress controller module to ensure cert-manager is installed after ingress."
  type        = any
}
