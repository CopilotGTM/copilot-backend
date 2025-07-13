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
  description = "Number of nodes in the default AKS node pool."
  type        = number
  default     = 2
}

variable "vm_size" {
  description = "VM size for the agent pool."
  type        = string
  default     = "Standard_DS2_v2"
}

variable "os_disk_size_gb" {
  description = "The size of the OS disk in GB for each node in the agent pool."
  type        = number
  default     = 128
}

variable "env" {
  description = "Deployment environment (used for tagging)."
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