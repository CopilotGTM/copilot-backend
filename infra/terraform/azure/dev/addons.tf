# -----------------------------------------------------------------
# 1) Managed Identity for cert-manager DNS challenges
# -----------------------------------------------------------------
resource "azurerm_user_assigned_identity" "cert_manager_dns" {
  name                = "cert-manager-dns-${var.env}"
  resource_group_name = module.resource_group.resource_group_name
  location            = var.location

  tags = {
    environment = var.env
    project     = var.project_name
    purpose     = "cert-manager-dns-challenge"
  }

  depends_on = [module.resource_group]
}

# Grant DNS Zone Contributor role to cert-manager identity
data "azurerm_dns_zone" "main" {
  name                = var.dns_zone_name
  resource_group_name = var.dns_zone_resource_group
}

resource "azurerm_role_assignment" "cert_manager_dns_contributor" {
  scope                = data.azurerm_dns_zone.main.id
  role_definition_name = "DNS Zone Contributor"
  principal_id         = azurerm_user_assigned_identity.cert_manager_dns.principal_id
}

# Federated Identity Credential for cert-manager to use the managed identity via Workload Identity
resource "azurerm_federated_identity_credential" "cert_manager" {
  name                = "cert-manager-federation"
  resource_group_name = module.resource_group.resource_group_name
  parent_id           = azurerm_user_assigned_identity.cert_manager_dns.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = module.aks.oidc_issuer_url
  subject             = "system:serviceaccount:cert-manager:cert-manager"

  depends_on = [module.aks]
}

# -----------------------------------------------------------------
# 2) Helm chart: cert-manager
# -----------------------------------------------------------------
resource "helm_release" "cert_manager" {
  name             = "cert-manager"
  repository       = "https://charts.jetstack.io"
  chart            = "cert-manager"
  namespace        = "cert-manager"
  version          = "1.18.2"
  create_namespace = true

  set = [
    {
      name  = "installCRDs"
      value = "true"
    },
    # Enable Azure Workload Identity for cert-manager pods
    # Use type = "string" to prevent boolean interpretation for labels
    {
      name  = "podLabels.azure\\.workload\\.identity/use"
      value = "true"
      type  = "string"
    },
    {
      name  = "serviceAccount.labels.azure\\.workload\\.identity/use"
      value = "true"
      type  = "string"
    },
    {
      name  = "serviceAccount.annotations.azure\\.workload\\.identity/client-id"
      value = azurerm_user_assigned_identity.cert_manager_dns.client_id
    },
    {
      name  = "serviceAccount.annotations.azure\\.workload\\.identity/tenant-id"
      value = data.azurerm_client_config.current.tenant_id
    }
  ]

  depends_on = [
    module.aks,
    azurerm_federated_identity_credential.cert_manager
  ]
}



# -----------------------------------------------------------------
# 2.1) Helm chart: KEDA (Kubernetes Event-driven Autoscaling)
# -----------------------------------------------------------------
resource "helm_release" "keda" {
  name             = "keda"
  repository       = "https://kedacore.github.io/charts"
  chart            = "keda"
  namespace        = "keda-system"
  version          = "2.15.1" # Latest stable version as of 2024
  create_namespace = true

  set = [
    {
      name  = "resources.operator.requests.cpu"
      value = "100m"
    },
    {
      name  = "resources.operator.requests.memory"
      value = "128Mi"
    },
    {
      name  = "resources.operator.limits.cpu"
      value = "1000m"
    },
    {
      name  = "resources.operator.limits.memory"
      value = "1000Mi"
    },
    {
      name  = "resources.metricServer.requests.cpu"
      value = "100m"
    },
    {
      name  = "resources.metricServer.requests.memory"
      value = "128Mi"
    },
    {
      name  = "resources.metricServer.limits.cpu"
      value = "1000m"
    },
    {
      name  = "resources.metricServer.limits.memory"
      value = "1000Mi"
    }
  ]

  depends_on = [module.aks]
}



# -----------------------------------------------------------------
# 3) cert-manager ClusterIssuer (Let’s Encrypt)
# -----------------------------------------------------------------
resource "time_sleep" "wait_for_cert_manager_crd" {
  create_duration = "30s"

  triggers = {
    cert_manager_release = helm_release.cert_manager.status
  }
}

# Using kubectl_manifest instead of kubernetes_manifest to avoid CRD validation during plan
resource "kubectl_manifest" "letsencrypt_issuer" {
  yaml_body = yamlencode({
    apiVersion = "cert-manager.io/v1"
    kind       = "ClusterIssuer"
    metadata = {
      name = "letsencrypt"
    }
    spec = {
      acme = {
        email  = var.letsencrypt_email
        server = "https://acme-v02.api.letsencrypt.org/directory"
        privateKeySecretRef = {
          name = "letsencrypt-account-key"
        }
        solvers = [
          {
            http01 = {
              ingress = {
                ingressClassName = "azure-application-gateway"
              }
            }
          },
          {
            dns01 = {
              azureDNS = {
                managedIdentity = {
                  clientID = azurerm_user_assigned_identity.cert_manager_dns.client_id
                }
                subscriptionID    = var.subscription_id
                resourceGroupName = var.dns_zone_resource_group
                hostedZoneName    = var.dns_zone_name
                environment       = "AzurePublicCloud"
              }
            }
          }
        ]
      }
    }
  })

  depends_on = [
    time_sleep.wait_for_cert_manager_crd,
    azurerm_role_assignment.cert_manager_dns_contributor
  ]
}



# -----------------------------------------------------------------
# 5) Create the application namespace
# -----------------------------------------------------------------
resource "kubernetes_namespace_v1" "app_namespace" {
  metadata {
    name = var.app_namespace
  }

  depends_on = [module.aks]
}

# -----------------------------------------------------------------
# 5.1) Envoy Gateway validation path
# -----------------------------------------------------------------
resource "kubernetes_namespace_v1" "envoy_gateway_system" {
  count = var.enable_envoy_gateway ? 1 : 0

  metadata {
    name = var.envoy_gateway_namespace

    labels = {
      name = var.envoy_gateway_namespace
    }
  }

  depends_on = [module.aks]
}

resource "azurerm_public_ip" "envoy_gateway" {
  count = var.enable_envoy_gateway ? 1 : 0

  name                = var.envoy_gateway_public_ip_name
  resource_group_name = module.aks.node_resource_group
  location            = var.location
  allocation_method   = "Static"
  sku                 = "Standard"

  tags = {
    environment = var.env
    project     = var.project_name
    purpose     = "envoy-gateway"
  }

  depends_on = [module.aks]
}

resource "helm_release" "envoy_gateway" {
  count = var.enable_envoy_gateway ? 1 : 0

  name       = "envoy-gateway"
  repository = "oci://docker.io/envoyproxy"
  chart      = "gateway-helm"
  namespace  = var.envoy_gateway_namespace
  version    = var.envoy_gateway_chart_version

  set = [
    {
      name  = "deployment.envoyGateway.resources.requests.cpu"
      value = "100m"
    },
    {
      name  = "deployment.envoyGateway.resources.requests.memory"
      value = "128Mi"
    },
    {
      name  = "deployment.envoyGateway.resources.limits.cpu"
      value = "500m"
    },
    {
      name  = "deployment.envoyGateway.resources.limits.memory"
      value = "512Mi"
    }
  ]

  depends_on = [
    kubernetes_namespace_v1.envoy_gateway_system
  ]
}

resource "kubectl_manifest" "letsencrypt_dns01_issuer" {
  yaml_body = yamlencode({
    apiVersion = "cert-manager.io/v1"
    kind       = "ClusterIssuer"
    metadata = {
      name = "letsencrypt-dns01"
    }
    spec = {
      acme = {
        email  = var.letsencrypt_email
        server = "https://acme-v02.api.letsencrypt.org/directory"
        privateKeySecretRef = {
          name = "letsencrypt-dns01-account-key"
        }
        solvers = [
          {
            dns01 = {
              azureDNS = {
                managedIdentity = {
                  clientID = azurerm_user_assigned_identity.cert_manager_dns.client_id
                }
                subscriptionID    = var.subscription_id
                resourceGroupName = var.dns_zone_resource_group
                hostedZoneName    = var.dns_zone_name
                environment       = "AzurePublicCloud"
              }
            }
          }
        ]
      }
    }
  })

  depends_on = [
    time_sleep.wait_for_cert_manager_crd,
    azurerm_role_assignment.cert_manager_dns_contributor
  ]
}

resource "kubectl_manifest" "envoy_proxy" {
  count = var.enable_envoy_gateway ? 1 : 0

  yaml_body = yamlencode({
    apiVersion = "gateway.envoyproxy.io/v1alpha1"
    kind       = "EnvoyProxy"
    metadata = {
      name      = "copilot-backend-dev"
      namespace = var.envoy_gateway_namespace
    }
    spec = {
      provider = {
        type = "Kubernetes"
        kubernetes = {
          envoyService = {
            type = "LoadBalancer"
            annotations = {
              "service.beta.kubernetes.io/azure-pip-name" = azurerm_public_ip.envoy_gateway[0].name
            }
          }
          envoyDeployment = {
            container = {
              resources = {
                requests = {
                  cpu    = "100m"
                  memory = "128Mi"
                }
                limits = {
                  cpu    = "500m"
                  memory = "512Mi"
                }
              }
            }
          }
        }
      }
    }
  })

  depends_on = [
    helm_release.envoy_gateway,
    azurerm_public_ip.envoy_gateway
  ]
}

resource "kubectl_manifest" "envoy_gateway_class" {
  count = var.enable_envoy_gateway ? 1 : 0

  yaml_body = yamlencode({
    apiVersion = "gateway.networking.k8s.io/v1"
    kind       = "GatewayClass"
    metadata = {
      name = var.envoy_gateway_class_name
    }
    spec = {
      controllerName = "gateway.envoyproxy.io/gatewayclass-controller"
      parametersRef = {
        group     = "gateway.envoyproxy.io"
        kind      = "EnvoyProxy"
        name      = "copilot-backend-dev"
        namespace = var.envoy_gateway_namespace
      }
    }
  })

  depends_on = [kubectl_manifest.envoy_proxy]
}



# -----------------------------------------------------------------
# 6) DNS A Record for API
# -----------------------------------------------------------------
resource "azurerm_dns_a_record" "api" {
  name                = var.api_subdomain
  zone_name           = var.dns_zone_name
  resource_group_name = var.dns_zone_resource_group
  ttl                 = 300
  records             = [azurerm_public_ip.envoy_gateway[0].ip_address]

  depends_on = [azurerm_public_ip.envoy_gateway]
}

resource "azurerm_dns_a_record" "api_envoy_validation" {
  count = var.enable_envoy_gateway ? 1 : 0

  name                = var.envoy_validation_subdomain
  zone_name           = var.dns_zone_name
  resource_group_name = var.dns_zone_resource_group
  ttl                 = 300
  records             = [azurerm_public_ip.envoy_gateway[0].ip_address]

  depends_on = [azurerm_public_ip.envoy_gateway]
}

# -----------------------------------------------------------------
# 7) Create a certificate for the API
# -----------------------------------------------------------------
# Using kubectl_manifest instead of kubernetes_manifest to avoid CRD validation during plan
resource "kubectl_manifest" "api_certificate" {
  yaml_body = yamlencode({
    apiVersion = "cert-manager.io/v1"
    kind       = "Certificate"
    metadata = {
      name      = "${var.api_subdomain}-tls"
      namespace = var.app_namespace
    }
    spec = {
      secretName = "${var.api_subdomain}-tls"
      issuerRef = {
        name = "letsencrypt-dns01"
        kind = "ClusterIssuer"
      }
      commonName = "${var.api_subdomain}.${var.dns_zone_name}"
      dnsNames   = ["${var.api_subdomain}.${var.dns_zone_name}"]
    }
  })

  depends_on = [
    kubectl_manifest.letsencrypt_issuer,
    kubectl_manifest.letsencrypt_dns01_issuer,
    kubernetes_namespace_v1.app_namespace
  ]
}

resource "kubectl_manifest" "api_envoy_validation_certificate" {
  count = var.enable_envoy_gateway ? 1 : 0

  yaml_body = yamlencode({
    apiVersion = "cert-manager.io/v1"
    kind       = "Certificate"
    metadata = {
      name      = "${var.envoy_validation_subdomain}-tls"
      namespace = var.app_namespace
    }
    spec = {
      secretName = "${var.envoy_validation_subdomain}-tls"
      issuerRef = {
        name = "letsencrypt-dns01"
        kind = "ClusterIssuer"
      }
      commonName = "${var.envoy_validation_subdomain}.${var.dns_zone_name}"
      dnsNames   = ["${var.envoy_validation_subdomain}.${var.dns_zone_name}"]
    }
  })

  depends_on = [
    kubectl_manifest.letsencrypt_dns01_issuer,
    kubernetes_namespace_v1.app_namespace,
    azurerm_dns_a_record.api_envoy_validation
  ]
}

