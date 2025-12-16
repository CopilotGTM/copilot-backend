# Nango Infrastructure on Azure AKS

This Terraform configuration deploys [Nango](https://github.com/NangoHQ/nango) - an open-source integration platform - on Azure Kubernetes Service (AKS) with supporting infrastructure.

## Overview

Nango is a unified API platform that helps you integrate with 300+ APIs through a single interface. This deployment provides a production-ready, cost-optimized single-environment setup.

### Architecture

- **Compute**: AKS cluster (1 node, Standard_B2ms: 2 vCPU, 8GB RAM)
- **Database**: Azure Database for PostgreSQL Flexible Server (B1ms: 1 vCore, 2GB RAM)
- **Cache**: Azure Cache for Redis (Basic C0: 250MB)
- **Storage**: Azure Key Vault for secrets
- **Networking**: Dedicated VNet (`nango-vnet-prod`) with NAT Gateway, NSG rules, ingress-nginx LoadBalancer
- **DNS**: A record for nango.copilotgtm.com

### Cost Estimate

**Monthly costs (West US region):**
- AKS (1x Standard_B2ms): ~$60-70/month
- PostgreSQL Flexible Server (B1ms): ~$15-20/month
- Redis Basic C0: ~$17/month
- Key Vault: ~$1/month
- Networking (NAT Gateway, Public IP, bandwidth): ~$40-50/month
- Storage: ~$2/month

**Total estimated cost: ~$150-180/month**

## Prerequisites

1. **Azure CLI** installed and authenticated:
   ```powershell
   az login
   az account set --subscription "675df0ed-8e06-441b-a1e4-110d19521816"
   ```

2. **Terraform** 1.3.0+ installed:
   ```powershell
   terraform version
   ```

3. **kubectl** for post-deployment verification:
   ```powershell
   az aks install-cli
   ```

4. **Required Azure permissions**:
   - Contributor on subscription
   - Key Vault Administrator role (for RBAC)

## Quick Start

### 1. Configure Variables

Edit `terraform.tfvars` if needed:
```hcl
subscription_id       = "675df0ed-8e06-441b-a1e4-110d19521816"
location              = "West US"
resource_group_name   = "copilot-nango"
env                   = "prod"
nango_host            = "nango.copilotgtm.com"
```

### 2. Initialize and Deploy

```powershell
cd d:\CopilotGTM\copilot-infra\terraform\azure\nango-prod

# Initialize Terraform
terraform init

# Review the deployment plan
terraform plan

# Deploy infrastructure (takes ~8-12 minutes)
terraform apply -auto-approve
```

### 3. Connect to AKS

```powershell
# Get AKS credentials
az aks get-credentials --resource-group copilot-nango-prod --name nango-aks-prod

# Verify cluster connectivity
kubectl get nodes
kubectl get pods -n nango
```

### 4. Access Nango

Once deployed, Nango will be available at:
- **HTTP**: http://nango.copilotgtm.com
- **API**: http://nango.copilotgtm.com/api

Wait 2-3 minutes for DNS propagation after deployment completes.

## Configuration

### Environment Variables

The deployment automatically configures these environment variables for Nango:

- `NANGO_DATABASE_URL`: PostgreSQL connection string
- `NANGO_REDIS_URL`: Redis connection string (rediss:// with SSL)
- `NANGO_ENCRYPTION_KEY`: Auto-generated 32-character encryption key
- `FLAG_SERVE_CONNECT_UI`: Enabled (true)

These are stored in a Kubernetes Secret named `nango-env` in the `nango` namespace.

### Nango Components

The Helm deployment includes:
- **Server** (port 3003): Main API server
- **Orchestrator** (port 3008): Job orchestration
- **Jobs** (port 3005): Background job processor
- **Persist** (port 3007): Data persistence service
- **Runner** (port 3006): Optional, disabled by default
- **Metering** (optional): Disabled by default

To enable optional components, set in `terraform.tfvars`:
```hcl
enable_runner = true
enable_metering = true
```

## Phased Deployment Plan

To make rollout and troubleshooting easier, deploy the stack in phased increments. Confirm each phase before moving to the next.

### Phase 0: Foundations
1. Resource Group (`copilot-nango-prod`)
2. Virtual Network stack (dedicated `nango-vnet-prod`, subnets, NSG, NAT Gateway, public IP)

### Phase 1: Platform Services
3. Key Vault (stores generated secrets)
4. Azure Database for PostgreSQL Flexible Server
5. Azure Cache for Redis

### Phase 2: Compute
6. AKS cluster (`nango-aks-prod`) with node pool joined to private subnet

### Phase 3: Kubernetes Add-ons
7. Ingress controller (`helm_release.ingress_nginx`)
8. Namespace and Secret (`kubernetes_namespace.nango`, `kubernetes_secret.nango_env`)
9. Nango Helm release (`helm_release.nango`)

### Phase 4: Edge
10. DNS A record (`nango.copilotgtm.com`) once ingress IP is known

For each phase, uncomment or add the relevant Terraform blocks, run `terraform plan`/`terraform apply`, and validate the deployment before proceeding.

## Troubleshooting

### Key Vault Name Conflict

If you see `VaultAlreadyExists` error:
- The configuration uses a random 4-character suffix (e.g., `nango-kv-prod-abc1`)
- If you recently deleted a Key Vault, it may be in soft-delete state
- Either wait for purge (7 days) or the random suffix will generate a new name

### Provider Inconsistency Errors

If you see "Provider produced inconsistent result" for VNet/NSG/NAT resources:
```powershell
# Option 1: Retry the apply
terraform apply -auto-approve

# Option 2: Apply with reduced parallelism
terraform apply -auto-approve -parallelism=1

# Option 3: Refresh and retry
terraform refresh
terraform apply -auto-approve
```

### PostgreSQL Region Restrictions

If deployment fails with `LocationIsOfferRestricted`:
- Update `location` in `terraform.tfvars` to a different region
- West US, East US 2, and Central US typically have availability

### DNS Not Resolving

If nango.copilotgtm.com doesn't resolve:
1. Check DNS record was created:
   ```powershell
   az network dns record-set a show --resource-group master-resources --zone-name copilotgtm.com --name nango
   ```

2. Wait 2-3 minutes for DNS propagation

3. Verify ingress IP:
   ```powershell
   kubectl get svc -n ingress-nginx
   ```

### Pod Issues

Check pod status:
```powershell
kubectl get pods -n nango
kubectl describe pod <pod-name> -n nango
kubectl logs <pod-name> -n nango
```

Common issues:
- **ImagePullBackOff**: Check AKS can pull from Docker Hub
- **CrashLoopBackOff**: Check logs for database/redis connection errors
- **Pending**: Check node resources with `kubectl describe node`

## Upgrading Nango

To upgrade to a newer Nango version:

1. Update the chart version in `main.tf`:
   ```hcl
   resource "helm_release" "nango" {
     chart   = "nango"
     version = "2.3.0"  # Update this
     ...
   }
   ```

2. Apply changes:
   ```powershell
   terraform apply -auto-approve
   ```

## Scaling

### Scale AKS Nodes

Update in `terraform.tfvars`:
```hcl
aks_node_count = 2  # Increase from 1
```

### Upgrade VM Size

```hcl
aks_vm_size = "Standard_B4ms"  # 4 vCPU, 16GB RAM
```

### Upgrade Database

```hcl
postgres_sku = "B_Standard_B2ms"  # 2 vCore, 4GB RAM
```

### Upgrade Redis

```hcl
redis_sku_name = "Standard"
redis_family   = "C"
redis_capacity = 1  # Standard C1: 1GB
```

## Backup and Disaster Recovery

### PostgreSQL Backups
- Automatic backups: Enabled, 7-day retention
- Point-in-time restore available

### Manual Backup
```powershell
# Export database
kubectl exec -n nango <nango-server-pod> -- pg_dump $NANGO_DATABASE_URL > nango-backup.sql

# Store in Azure Blob Storage
az storage blob upload --account-name <storage> --container backups --file nango-backup.sql
```

## Security Considerations

1. **Public Access**: Database and Redis allow public access with passwords
   - For production, consider:
     - VNet integration for PostgreSQL
     - Private endpoints for Redis
     - Azure Private Link

2. **Key Vault**: Uses RBAC with 7-day soft delete and purge protection

3. **Secrets**: Database password stored in Key Vault, injected to Kubernetes Secret

4. **Network**: NSG rules allow HTTP/HTTPS and LoadBalancer traffic only

## CI/CD Integration

### Azure DevOps Pipeline Example

```yaml
trigger:
  branches:
    include:
    - main
  paths:
    include:
    - terraform/azure/nango-prod/**

pool:
  vmImage: 'ubuntu-latest'

steps:
- task: TerraformInstaller@0
  inputs:
    terraformVersion: '1.3.0'

- task: TerraformTaskV4@4
  inputs:
    provider: 'azurerm'
    command: 'init'
    workingDirectory: '$(System.DefaultWorkingDirectory)/terraform/azure/nango-prod'
    backendServiceArm: 'Azure-Connection'
    backendAzureRmResourceGroupName: 'terraform-state-rg'
    backendAzureRmStorageAccountName: 'gtmstatestor001'
    backendAzureRmContainerName: 'tfstate'
    backendAzureRmKey: 'azure/nango-prod/terraform.tfstate'

- task: TerraformTaskV4@4
  inputs:
    provider: 'azurerm'
    command: 'apply'
    workingDirectory: '$(System.DefaultWorkingDirectory)/terraform/azure/nango-prod'
    environmentServiceNameAzureRM: 'Azure-Connection'
    commandOptions: '-auto-approve'
```

### GitHub Actions Example

```yaml
name: Deploy Nango Infrastructure

on:
  push:
    branches: [main]
    paths:
      - 'terraform/azure/nango-prod/**'

jobs:
  terraform:
    runs-on: ubuntu-latest
    
    steps:
    - uses: actions/checkout@v3
    
    - uses: hashicorp/setup-terraform@v2
      with:
        terraform_version: 1.3.0
    
    - name: Azure Login
      uses: azure/login@v1
      with:
        creds: ${{ secrets.AZURE_CREDENTIALS }}
    
    - name: Terraform Init
      run: terraform init
      working-directory: terraform/azure/nango-prod
    
    - name: Terraform Apply
      run: terraform apply -auto-approve
      working-directory: terraform/azure/nango-prod
      env:
        ARM_SUBSCRIPTION_ID: ${{ secrets.AZURE_SUBSCRIPTION_ID }}
```

## Monitoring

### AKS Monitoring
```powershell
# View cluster metrics
az aks show --resource-group copilot-nango-prod --name nango-aks-prod

# View node metrics
kubectl top nodes

# View pod metrics
kubectl top pods -n nango
```

### Application Logs
```powershell
# Follow Nango server logs
kubectl logs -f -n nango -l app=nango-server

# View all component logs
kubectl logs -n nango --all-containers=true -l app.kubernetes.io/name=nango
```

### Azure Monitor
Enable Container Insights for advanced monitoring:
```powershell
az aks enable-addons --resource-group copilot-nango-prod --name nango-aks-prod --addons monitoring
```

## Outputs

After deployment, Terraform outputs:
- `resource_group_name`: Resource group containing all resources
- `aks_name`: AKS cluster name
- `aks_host`: AKS API server endpoint (sensitive)
- `nango_host`: Nango hostname (nango.copilotgtm.com)
- `nango_url`: Full Nango URL (once Stage 2 is deployed)
- `ingress_ip`: LoadBalancer public IP (once Stage 2 is deployed)

View outputs:
```powershell
terraform output
terraform output -json
```

## Destroying the Infrastructure

To tear down all resources:

```powershell
terraform destroy -auto-approve
```

**Note**: Key Vault will be soft-deleted and retained for 7 days due to purge protection. This is by design for security.

## Support and Resources

- **Nango Documentation**: https://docs.nango.dev
- **Nango GitHub**: https://github.com/NangoHQ/nango
- **Nango Helm Chart**: https://github.com/NangoHQ/nango-helm-charts
- **Azure AKS Docs**: https://learn.microsoft.com/azure/aks/

## License

This Terraform configuration is provided as-is. Nango is licensed under the Elastic License 2.0.
