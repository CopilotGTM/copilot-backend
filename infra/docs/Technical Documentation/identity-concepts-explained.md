# Identity & Authentication Concepts

## Quick Reference

| Concept | What It Is | Analogy |
|---------|-----------|---------|
| **App Registration** | An identity record in Entra ID (Azure AD) for your application | Like registering a company name |
| **Service Principal** | The actual "user account" for that app in your tenant | Like the company's employee badge |
| **Client Secret** | A password for the service principal | A password that expires & needs rotation |
| **OIDC / Workload Identity Federation** | Passwordless auth using tokens | Like using fingerprint instead of password |
| **Service Connection** | Azure DevOps link to Azure using the above identity | The "saved login" in Azure DevOps |
| **Federated Credential** | Trust relationship allowing OIDC token exchange | "Trust this fingerprint scanner" |

---

## How They Connect

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           ENTRA ID (Azure AD)                           │
│  ┌──────────────────┐     ┌─────────────────────┐                       │
│  │  App Registration │────▶│  Service Principal  │                       │
│  │  (identity card)  │     │  (the actual user)  │                       │
│  └──────────────────┘     └─────────────────────┘                       │
│           │                         │                                    │
│           │                         ├── Client Secret (password) ──OR── │
│           │                         │                                    │
│           └── Federated Credential ─┘   (OIDC - no password)            │
└─────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                            AZURE DEVOPS                                  │
│  ┌────────────────────────────────────────┐                             │
│  │  Service Connection                     │                             │
│  │  - Points to the Service Principal      │                             │
│  │  - Uses Secret OR OIDC to authenticate  │                             │
│  └────────────────────────────────────────┘                             │
└─────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                         AZURE SUBSCRIPTION                               │
│  Service Principal has RBAC roles:                                       │
│  - Contributor (create/manage resources)                                 │
│  - User Access Administrator (assign roles to others)                    │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Secret-Based vs OIDC Authentication

| Aspect | Client Secret | OIDC (Workload Identity Federation) |
|--------|---------------|-------------------------------------|
| **Security** | Password stored in Azure DevOps | No password - uses short-lived tokens |
| **Rotation** | Must rotate every 1-2 years | Never - tokens auto-refresh |
| **Setup** | Simpler | Requires federated credential config |
| **Best Practice** | Legacy | Microsoft recommended for production |

---

## In Our Setup

1. **module.service_principal** creates:
   - App Registration + Service Principal in Entra ID
   - RBAC roles (Contributor, User Access Administrator)
   - Federated Credential (for OIDC)

2. **module.service_connection** creates:
   - Azure DevOps Service Connection pointing to the SP
   - Uses OIDC (no secrets to manage)

3. **Pipeline** uses the service connection to authenticate to Azure and run Terraform.
