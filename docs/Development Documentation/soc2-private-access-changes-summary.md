# SOC2 Private Access - Changes Summary

**Date**: December 22, 2025  
**Environment**: Dev (`terraform/azure/dev`)  
**Status**: Deployed ✅

---

## Summary of Changes

This document summarizes the infrastructure changes made to support SOC2 compliance by enabling private network access to Azure PaaS services.

## 1. VNet Module Changes

### Files Modified
- `terraform/azure/modules/vnet/main.tf`
- `terraform/azure/modules/vnet/variables.tf`
- `terraform/azure/modules/vnet/outputs.tf`

### What Was Added

**New Subnet for Private Endpoints**
```hcl
resource "azurerm_subnet" "private_endpoints" {
  count                = var.private_endpoints_subnet_prefix != "" ? 1 : 0
  name                 = "${var.vnet_name}-private-endpoints"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [var.private_endpoints_subnet_prefix]

  private_endpoint_network_policies = "Disabled"
}
```

**New Variable**
```hcl
variable "private_endpoints_subnet_prefix" {
  description = "Optional CIDR prefix for a dedicated private endpoints subnet."
  type        = string
  default     = ""
}
```

**New Outputs**
- `private_endpoints_subnet_id`
- `private_endpoints_subnet_name`

---

## 2. Key Vault Module Changes

### Files Modified
- `terraform/azure/modules/key_vault/main.tf`
- `terraform/azure/modules/key_vault/variables.tf`
- `terraform/azure/modules/key_vault/outputs.tf`

### What Was Added

**Private Endpoint Support**
```hcl
resource "azurerm_private_endpoint" "this" {
  count               = var.private_endpoint_enabled ? 1 : 0
  name                = local.private_endpoint_name
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.private_endpoint_subnet_id

  private_service_connection {
    name                           = "${local.private_endpoint_name}-psc"
    is_manual_connection           = false
    private_connection_resource_id = azurerm_key_vault.this.id
    subresource_names              = ["vault"]
  }

  dynamic "private_dns_zone_group" {
    for_each = var.private_dns_zone_id != null ? [1] : []
    content {
      name                 = "default"
      private_dns_zone_ids = [var.private_dns_zone_id]
    }
  }
}
```

**New Variables**
| Variable | Type | Default | Purpose |
|----------|------|---------|---------|
| `public_network_access_enabled` | bool | true | Control public access |
| `private_endpoint_enabled` | bool | false | Enable private endpoint |
| `private_endpoint_subnet_id` | string | null | Subnet for PE |
| `private_dns_zone_id` | string | null | DNS zone for PE |
| `private_endpoint_name` | string | "" | Custom PE name |

---

## 3. Dev Environment Wiring

### Files Modified
- `terraform/azure/dev/main.tf`
- `terraform/azure/dev/variables.tf`
- `terraform/azure/dev/terraform.tfvars`
- `terraform/azure/dev/provider.tf`

### Configuration Added

**terraform.tfvars**
```hcl
# Dedicated subnet for Private Link private endpoints
vnet_private_endpoints_subnet = "10.0.4.0/24"

# Private Link rollout toggles (keep false until Terraform runs inside the VNet)
enable_key_vault_private_endpoints = false
```

**New Variables in variables.tf**
```hcl
variable "vnet_private_endpoints_subnet" {
  description = "CIDR for a dedicated private endpoints subnet"
  type        = string
  default     = "10.0.4.0/24"
}

variable "enable_key_vault_private_endpoints" {
  description = "Enable Private Link private endpoints for Key Vaults"
  type        = bool
  default     = false
}
```

**Azure DevOps PAT Key Vault Support (provider.tf)**
```hcl
data "azurerm_key_vault_secret" "azuredevops_pat" {
  count        = var.azuredevops_pat == "" && var.azuredevops_pat_secret_name != "" ? 1 : 0
  name         = var.azuredevops_pat_secret_name
  key_vault_id = module.master_key_vault.key_vault_id
}

locals {
  effective_azuredevops_pat = var.azuredevops_pat != "" ? var.azuredevops_pat : try(data.azurerm_key_vault_secret.azuredevops_pat[0].value, null)
}
```

---

## 4. What Got Deployed

After `terraform apply`, the following resources were created/modified:

| Resource | Action | Details |
|----------|--------|---------|
| `azurerm_subnet.private_endpoints[0]` | Created | 10.0.4.0/24 subnet |
| `azurerm_linux_web_app` (frontend) | Updated | Image tag update |
| `azurerm_linux_web_app` (admin) | Updated | Image tag update |
| `azuread_service_principal_password` | Replaced | SP credential rotation |
| `azuredevops_serviceendpoint_*` | Updated | New SP credentials |
| `azuredevops_build_definition` (3x) | Updated | Branch name normalization |

---

## 5. Current Network Architecture

```
VNet: copilot-gtm-vnet-dev (10.0.0.0/16)
├── copilot-gtm-vnet-dev-public-1     (10.0.1.0/24) - Internet-facing
├── copilot-gtm-vnet-dev-private-1    (10.0.2.0/24) - AKS/Microservices
├── copilot-gtm-vnet-dev-private-2    (10.0.3.0/24) - Redis/Data
└── copilot-gtm-vnet-dev-private-endpoints (10.0.4.0/24) - Private Link [NEW]
```

---

## 6. How to Enable Private Access (Future)

When you're ready to enable private endpoints (requires Terraform runner inside VNet):

```hcl
# In terraform.tfvars
enable_key_vault_private_endpoints = true
```

This will:
1. Create Private DNS Zone (`privatelink.vaultcore.azure.net`)
2. Link DNS Zone to VNet
3. Create Private Endpoint for each Key Vault
4. Disable public network access on Key Vaults

---

## 7. Next Steps (SOC2 Roadmap)

| Priority | Service | Status |
|----------|---------|--------|
| 1 | Key Vault | ✅ Module ready, toggle OFF |
| 2 | Storage Account | 🔲 Pending |
| 3 | ACR | 🔲 Pending |
| 4 | PostgreSQL | 🔲 Pending |
| 5 | Redis | 🔲 Pending |

---

## Related Documentation

- [VNet Module Details](./Module%20Details/vnet-module.md)
- [Private Access Implementation Tracker](./private-access-implementation-tracker.md)
- [SOC2 Infrastructure Production Readiness](./soc2-infra-production-readiness.md)
