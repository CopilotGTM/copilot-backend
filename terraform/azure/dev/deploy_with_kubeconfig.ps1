# PowerShell script to automate AKS infra deployment and kubeconfig extraction

# Step 1: Deploy Azure infra and AKS
Write-Host "Running terraform apply in terraform/azure/dev/ ..."
cd "$(Split-Path -Parent $MyInvocation.MyCommand.Definition)"
terraform apply -auto-approve

if ($LASTEXITCODE -ne 0) {
    Write-Error "Terraform apply failed. Exiting."
    exit 1
}

# Step 2: Extract kubeconfig output and save to k8s directory
$kubeconfig = terraform output -raw kube_config 2>$null
if (-not $kubeconfig) {
    Write-Error "Failed to extract kube_config output. Make sure the output variable is defined in outputs.tf."
    exit 1
}

$k8sDir = "k8s"
if (-not (Test-Path $k8sDir)) {
    New-Item -ItemType Directory -Path $k8sDir | Out-Null
}

$kubeconfigPath = Join-Path $k8sDir "kubeconfig"
$kubeconfig | Out-File -FilePath $kubeconfigPath -Encoding ascii

Write-Host "Kubeconfig has been saved to terraform/azure/dev/k8s/kubeconfig."
Write-Host "You can now run 'terraform apply' in terraform/azure/dev/k8s/ to deploy Kubernetes resources."
