# KEDA Integration Guide

This document explains how to use KEDA (Kubernetes Event-driven Autoscaling) in your infrastructure.

## What is KEDA?

KEDA is a Kubernetes-based Event Driven Autoscaler that allows you to scale applications based on various external metrics like:
- Azure Service Bus queues
- Redis lists/streams
- HTTP requests
- Database queries
- Custom metrics
- And many more

## Installation

KEDA is installed via Terraform in the `terraform/azure/dev/k8s/main.tf` file using the official Helm chart.

## Configuration

### 1. Enable KEDA in Terraform

Set the following variables in your `terraform.tfvars`:

```hcl
enable_keda_autoscaling = true
keda_polling_interval = 30      # How often KEDA checks metrics (seconds)
keda_cooldown_period = 300      # Time to wait before scaling down (seconds)
```

### 2. Enable KEDA in your Helm Chart

In your `values.yaml` for the technical-deck chart:

```yaml
# Disable standard HPA
autoscaling:
  enabled: false

# Enable KEDA autoscaling
kedaAutoscaling:
  enabled: true
  minReplicas: 1
  maxReplicas: 10
  pollingInterval: 30
  cooldownPeriod: 300
  triggers:
    # CPU-based scaling
    - type: cpu
      metadata:
        type: Utilization
        value: "60"
    # Memory-based scaling  
    - type: memory
      metadata:
        type: Utilization
        value: "70"
```

## Common KEDA Triggers

### Azure Service Bus Queue
```yaml
triggers:
  - type: azure-servicebus
    metadata:
      queueName: "myqueue"
      connectionFromEnv: "SERVICEBUS_CONNECTION_STRING"
      queueLength: "5"
```

### Redis List/Stream
```yaml
triggers:
  - type: redis
    metadata:
      address: "redis-service:6379"
      listName: "mylist"
      listLength: "5"
```

### HTTP Requests (Prometheus)
```yaml
triggers:
  - type: prometheus
    metadata:
      serverAddress: "http://prometheus:9090"
      metricName: "http_requests_per_second"
      threshold: "100"
      query: "sum(rate(http_requests_total[1m]))"
```

### Azure Blob Storage
```yaml
triggers:
  - type: azure-blob
    metadata:
      connectionFromEnv: "AZURE_STORAGE_CONNECTION_STRING"
      containerName: "mycontainer"
      blobCount: "10"
```

## Environment Variables

Add the necessary connection strings to your deployment's environment variables:

```yaml
env:
  - name: SERVICEBUS_CONNECTION_STRING
    valueFrom:
      secretKeyRef:
        name: azure-secrets
        key: servicebus-connection
  - name: AZURE_STORAGE_CONNECTION_STRING
    valueFrom:
      secretKeyRef:
        name: azure-secrets
        key: storage-connection
```

## Best Practices

1. **Start Small**: Begin with CPU/Memory triggers and gradually add external metrics
2. **Monitor Costs**: External metrics can increase your Azure costs
3. **Set Appropriate Thresholds**: Avoid too aggressive scaling to prevent oscillation
4. **Use Secrets**: Store connection strings in Kubernetes secrets
5. **Test Thoroughly**: Test scaling behavior in non-production environments first

## Troubleshooting

### Check KEDA Status
```bash
kubectl get scaledobjects -n your-namespace
kubectl describe scaledobject your-app-scaledobject -n your-namespace
```

### Check KEDA Logs
```bash
kubectl logs -n keda-system deployment/keda-operator
kubectl logs -n keda-system deployment/keda-metrics-apiserver
```

### Check HPA Status (created by KEDA)
```bash
kubectl get hpa -n your-namespace
kubectl describe hpa keda-hpa-your-app -n your-namespace
```

## Migration from Standard HPA

1. Set `autoscaling.enabled: false` in your values.yaml
2. Set `kedaAutoscaling.enabled: true` in your values.yaml
3. Configure appropriate triggers
4. Deploy and monitor

Note: KEDA creates its own HPA objects, so disable the standard HPA to avoid conflicts.
