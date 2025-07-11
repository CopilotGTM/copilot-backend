# modules/cert-manager/main.tf

terraform {
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
    }
    helm = {
      source  = "hashicorp/helm"
    }
  }
}

# Create namespace for cert-manager
resource "kubernetes_namespace" "cert_manager" {
  metadata {
    name = var.namespace
    labels = {
      "cert-manager.io/disable-validation" = "true"
    }
  }
}

# Install cert-manager using Helm
resource "helm_release" "cert_manager" {
  name       = var.release_name
  repository = "https://charts.jetstack.io"
  chart      = "cert-manager"
  namespace  = var.namespace
  version    = var.chart_version
  
  timeout = 600
  wait    = true
  wait_for_jobs = true
  
  # Install CRDs
  set = [{
    name  = "installCRDs"
    value = "true"
  }
  
  # Enable webhook
  ,{
    name  = "webhook.enabled"
    value = "true"
  }
  
  # Enable cainjector
  ,{
    name  = "cainjector.enabled"
    value = "true"
  }
  
  # Set resource limits
  ,{
    name  = "resources.limits.cpu"
    value = "100m"
  }

  ,{
    name  = "resources.limits.memory"
    value = "128Mi"
  }
  
  ,{
    name  = "resources.requests.cpu"
    value = "10m"
  }
  
  ,{
    name  = "resources.requests.memory"
    value = "32Mi"
  }
  ]
  
  depends_on = [kubernetes_namespace.cert_manager]
}

# Wait for cert-manager to be ready AND CRDs to be available
resource "null_resource" "wait_for_cert_manager" {
  depends_on = [helm_release.cert_manager]
  
  provisioner "local-exec" {
    command = <<-EOT
      echo "Waiting for cert-manager CRDs to be available..."
      # Wait for CRDs to be established
      kubectl wait --for=condition=Established crd/clusterissuers.cert-manager.io --timeout=300s
      kubectl wait --for=condition=Established crd/certificates.cert-manager.io --timeout=300s
      kubectl wait --for=condition=Established crd/certificaterequests.cert-manager.io --timeout=300s
      
      echo "Waiting for cert-manager deployments to be ready..."
      kubectl wait --for=condition=Available deployment/cert-manager -n ${var.namespace} --timeout=300s
      kubectl wait --for=condition=Available deployment/cert-manager-cainjector -n ${var.namespace} --timeout=300s
      kubectl wait --for=condition=Available deployment/cert-manager-webhook -n ${var.namespace} --timeout=300s
      
      echo "Cert-manager is ready!"
    EOT
  }
}

# Create ClusterIssuer for Let's Encrypt Production using kubectl
resource "null_resource" "letsencrypt_prod_issuer" {
  depends_on = [null_resource.wait_for_cert_manager]
  
  provisioner "local-exec" {
    command = <<-EOT
      cat <<EOF | kubectl apply -f -
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: ${var.letsencrypt_prod_issuer_name}
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: ${var.letsencrypt_email}
    privateKeySecretRef:
      name: ${var.letsencrypt_prod_issuer_name}
    solvers:
    - http01:
        ingress:
          class: ${var.ingress_class}
EOF
    EOT
  }
  
  provisioner "local-exec" {
    when    = destroy
    command = "kubectl delete clusterissuer ${self.triggers.issuer_name} --ignore-not-found=true"
  }
  
  triggers = {
    issuer_name = var.letsencrypt_prod_issuer_name
    email = var.letsencrypt_email
    ingress_class = var.ingress_class
  }
}

# Create ClusterIssuer for Let's Encrypt Staging using kubectl
resource "null_resource" "letsencrypt_staging_issuer" {
  count      = var.create_staging_issuer ? 1 : 0
  depends_on = [null_resource.wait_for_cert_manager]
  
  provisioner "local-exec" {
    command = <<-EOT
      cat <<EOF | kubectl apply -f -
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: ${var.letsencrypt_staging_issuer_name}
spec:
  acme:
    server: https://acme-staging-v02.api.letsencrypt.org/directory
    email: ${var.letsencrypt_email}
    privateKeySecretRef:
      name: ${var.letsencrypt_staging_issuer_name}
    solvers:
    - http01:
        ingress:
          class: ${var.ingress_class}
EOF
    EOT
  }
  
  provisioner "local-exec" {
    when    = destroy
    command = "kubectl delete clusterissuer ${self.triggers.issuer_name} --ignore-not-found=true"
  }
  
  triggers = {
    issuer_name = var.letsencrypt_staging_issuer_name
    email = var.letsencrypt_email
    ingress_class = var.ingress_class
  }
}

# Create certificates based on the certificates variable using kubectl
resource "null_resource" "certificates" {
  for_each   = var.certificates
  depends_on = [null_resource.letsencrypt_prod_issuer]
  
  provisioner "local-exec" {
    command = <<-EOT
      cat <<EOF | kubectl apply -f -
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: ${each.value.name}
  namespace: ${each.value.namespace}
spec:
  secretName: ${each.value.secret_name}
  issuerRef:
    name: ${each.value.issuer_name}
    kind: ${each.value.issuer_kind}
  commonName: ${each.value.common_name}
  dnsNames:
${join("\n", [for dns in each.value.dns_names : "  - ${dns}"])}
EOF
    EOT
  }
  
  provisioner "local-exec" {
    when    = destroy
    command = "kubectl delete certificate ${self.triggers.name} -n ${self.triggers.namespace} --ignore-not-found=true"
  }
  
  triggers = {
    name = each.value.name
    namespace = each.value.namespace
    secret_name = each.value.secret_name
    issuer_name = each.value.issuer_name
    issuer_kind = each.value.issuer_kind
    common_name = each.value.common_name
    dns_names = join(",", each.value.dns_names)
  }
}

# Create a test certificate if enabled using kubectl
resource "null_resource" "test_certificate" {
  count      = var.create_test_certificate ? 1 : 0
  depends_on = [null_resource.letsencrypt_staging_issuer]
  
  provisioner "local-exec" {
    command = <<-EOT
      cat <<EOF | kubectl apply -f -
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: test-certificate
  namespace: ${var.test_certificate_namespace}
spec:
  secretName: test-certificate-tls
  issuerRef:
    name: ${var.letsencrypt_staging_issuer_name}
    kind: ClusterIssuer
  commonName: ${var.test_certificate_domain}
  dnsNames:
  - ${var.test_certificate_domain}
EOF
    EOT
  }
  
  provisioner "local-exec" {
    when    = destroy
    command = "kubectl delete certificate test-certificate -n ${self.triggers.namespace} --ignore-not-found=true"
  }
  
  triggers = {
    domain = var.test_certificate_domain
    namespace = var.test_certificate_namespace
    issuer_name = var.letsencrypt_staging_issuer_name
  }
}