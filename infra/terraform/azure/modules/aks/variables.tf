variable "aks_name" {
  description = "The name of the AKS cluster."
  type        = string
}

variable "dns_prefix" {
  description = "The DNS prefix for the AKS cluster."
  type        = string
}

variable "location" {
  description = "Location for the AKS cluster."
  type        = string
}

variable "resource_group_name" {
  description = "Resource group name for AKS."
  type        = string
}

variable "vnet_subnet_ids" {
  description = "A list of subnet IDs where the AKS agent nodes will be placed."
  type        = list(string)
}

variable "agent_count" {
  description = "Number of nodes in the default AKS node pool (used when autoscaling is disabled)."
  type        = number
  default     = 2
}

variable "enable_auto_scaling" {
  description = "Enable autoscaling on the default node pool."
  type        = bool
  default     = false
}

variable "min_count" {
  description = "Minimum number of nodes when autoscaling is enabled."
  type        = number
  default     = 1
}

variable "max_count" {
  description = "Maximum number of nodes when autoscaling is enabled."
  type        = number
  default     = 3
}

variable "vm_size" {
  description = "VM size for the agent pool."
  type        = string
  default     = "Standard_DS2_v2"
}

variable "max_pods" {
  description = "Maximum number of pods that can run on each default node pool node. Null keeps the Azure default."
  type        = number
  default     = null
}

variable "os_disk_size_gb" {
  description = "The size of the OS disk in GB for each node in the agent pool."
  type        = number
  default     = 128
}

variable "env" {
  description = "The environment name (e.g., dev, uat, prod)."
  type        = string
}

variable "service_cidr" {
  description = "The Kubernetes service address range (must NOT overlap vnet/subnets). Example: 10.1.0.0/16"
  type        = string
  default     = "10.1.0.0/16"
}

variable "dns_service_ip" {
  description = "The Kubernetes DNS service IP (must be inside service_cidr, and not overlap VNet). Example: 10.1.0.10"
  type        = string
  default     = "10.1.0.10"
}

variable "outbound_type" {
  description = "AKS outbound egress type. Use userAssignedNATGateway when AKS subnets already have a NAT Gateway."
  type        = string
  default     = "loadBalancer"

  validation {
    condition = contains([
      "loadBalancer",
      "userAssignedNATGateway",
      "userDefinedRouting",
      "none",
    ], var.outbound_type)
    error_message = "outbound_type must be loadBalancer, userAssignedNATGateway, userDefinedRouting, or none."
  }
}

variable "workload_identity_enabled" {
  description = "Enable workload identity"
  type        = bool
  default     = true
}

variable "oidc_issuer_enabled" {
  description = "Enable OIDC issuer"
  type        = bool
  default     = true
}

# -----------------------------------------------------------------------------
# Optional GPU (knowledge worker) node pool
# -----------------------------------------------------------------------------
variable "enable_gpu_worker_pool" {
  description = "Whether to create a GPU node pool dedicated to knowledge workers (autoscaling 0->N)."
  type        = bool
  default     = false
}

variable "gpu_node_pool_name" {
  description = "Name of the GPU node pool (must be 1-12 chars, lowercase letters & numbers)."
  type        = string
  default     = "kgpu"
}

variable "gpu_vm_size" {
  description = "Azure VM size/SKU for GPU nodes (must be available in region)."
  type        = string
  default     = "Standard_NC4as_T4_v3"
}

variable "gpu_min_count" {
  description = "Minimum number of GPU nodes (0 enables scale-to-zero)."
  type        = number
  default     = 0
}

variable "gpu_max_count" {
  description = "Maximum number of GPU nodes to autoscale to."
  type        = number
  default     = 1
}

variable "gpu_node_labels" {
  description = "Map of Kubernetes node labels to apply to the GPU node pool."
  type        = map(string)
  default = {
    "workload" = "knowledge-gpu"
  }
}

variable "gpu_node_taints" {
  description = "List of taints to apply to the GPU node pool to restrict scheduling."
  type        = list(string)
  default     = ["dedicated=gpu:NoSchedule"]
}
