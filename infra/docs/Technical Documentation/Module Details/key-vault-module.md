# Key Vault Module Documentation

## Overview

The Key Vault module provisions an Azure Key Vault with RBAC, mandatory Private Link (Private Endpoint), and Private DNS integration. It supports SOC2 by disabling public access and ensuring traffic flows only over Private Link.

## What It Creates

- Key Vault (RBAC enabled by default)
- Private Endpoint (PE) for `vault` subresource (mandatory)
- Private DNS zone group attachment to the PE (mandatory)
- Role assignment for the specified admin principal

## Inputs

| Variable | Type | Default | Purpose |
|----------|------|---------|---------|
| `key_vault_name` | string | n/a | Vault name |
| `location` | string | n/a | Azure region |
| `resource_group_name` | string | n/a | Resource group |
| `tenant_id` | string | n/a | AAD tenant |
| `admin_object_id` | string | n/a | Admin principal object ID |
| `enable_rbac_authorization` | bool | true | Use RBAC instead of access policies |
| `public_network_access_enabled` | bool | false | Must be `false` (public access disabled) |
| `private_endpoint_enabled` | bool | true | Must be `true` (Private Link required) |
| `private_endpoint_subnet_id` | string | null | Required: subnet for the Private Endpoint |
| `private_dns_zone_id` | string | null | Required: Private DNS zone ID (e.g., privatelink.vaultcore.azure.net) |
| `private_endpoint_name` | string | "" | Optional PE name override |

## Outputs

| Output | Description |
|--------|-------------|
| `key_vault_id` | Vault ID |
| `key_vault_uri` | Vault URI |
| `private_endpoint_id` | Private Endpoint ID (if created) |
| `private_endpoint_private_ip` | Private Endpoint IP (if created) |

## How Private Endpoints Are Used (SOC2)

1) Create a dedicated PE subnet (done in VNet module).  
2) Create a Private DNS zone `privatelink.vaultcore.azure.net` and link it to the VNet.  
3) Set `private_endpoint_enabled = true` and point to the PE subnet + DNS zone.  
4) Set `public_network_access_enabled = false` (module enforces this).  
5) Vault traffic resolves to the PE IP inside the VNet; no public ingress.

## Current Dev Wiring

- Toggle: `enable_key_vault_private_endpoints = true` (required by module validations).  
- PE subnet: `10.0.4.0/24` (from VNet module).  
- DNS zone creation/linking is in `infra/terraform/azure/dev/main.tf`.

## Minimal Usage Example

```hcl
module "key_vault" {
  source                  = "../modules/key_vault"
  key_vault_name          = "app-master-vault-${var.env}"
  location                = var.location
  resource_group_name     = module.resource_group.resource_group_name
  tenant_id               = var.subscription_tenant_id
  admin_object_id         = var.admin_object_id
  enable_rbac_authorization = true

  # Private access (mandatory)
  private_endpoint_enabled   = true
  private_endpoint_subnet_id = module.vnet.private_endpoints_subnet_id
  private_dns_zone_id        = azurerm_private_dns_zone.key_vault[0].id
}
```

---

## FAQ

**Q: Why force public access off when the private endpoint is on?**  
A: To ensure all vault traffic stays on Private Link; leaving public access on would undermine SOC2 private-access intent.

**Q: What DNS is required for the Key Vault private endpoint?**  
A: A Private DNS zone `privatelink.vaultcore.azure.net` linked to the VNet. Without it, clients will hit the public endpoint even if a PE exists.

**Q: Can I use the same Private DNS zone for multiple vaults?**  
A: Yes. Add multiple PE zone group attachments to the same zone; records are auto-managed.

**Q: How does traffic flow?**  
A: Workloads in private subnets resolve the vault FQDN to the PE IP in the PE subnet, then connect over the VNet. No internet path is used.

**Q: What happens if the PE subnet is missing or full?**  
A: PE creation will fail. Ensure the dedicated PE subnet exists with available IPs and `private_endpoint_network_policies = "Disabled"`.

**Q: How do I roll back to public access?**  
A: This module intentionally does not support public access. If you truly need public access temporarily, you must change the module validations (or create a separate “public” Key Vault module) and re-apply.

**Q: Are keys/secrets soft-deleted?**  
A: Yes, Key Vault has soft delete and purge protection enabled by default in the module.
