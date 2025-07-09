# Only variables required by k8s-apps modules (all root-level settings come from main.tf)
# Values should be passed from main.tf, referenced via module entry, populated by terraform.tfvars at the root level

variable "aks_dependency" {
  description = "AKS dependency object from parent module"
  type        = any
}

variable "ingress_controller_dependency" {
  description = "Ingress controller dependency object from parent module"
  type        = any
}

variable "namespace" {
  description = "Namespace to be used for cert-manager and issuers"
  type        = string
}

variable "dns_record_dependency" {
  description = "Dependency on DNS A record, passed from root"
  type        = any
}
