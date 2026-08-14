"""Production AKS capacity contracts for deployment and autoscaling control planes."""

import re
from pathlib import Path

PROD_TFVARS = Path("infra/terraform/azure/prod/terraform.tfvars")
PROD_ADDONS = Path("infra/terraform/azure/prod/addons.tf")


def test_prod_node_pool_keeps_one_node_of_autoscaling_headroom() -> None:
    """The steady three-node workload must be able to trigger a fourth node."""

    tfvars = PROD_TFVARS.read_text(encoding="utf-8")
    match = re.search(r"^aks_max_node_count\s*=\s*(\d+)\s*$", tfvars, re.MULTILINE)

    assert match is not None
    assert int(match.group(1)) >= 4


def test_keda_control_plane_cannot_be_preempted_by_application_workers() -> None:
    """Autoscaling must remain available while application Pods compete for CPU."""

    addons = PROD_ADDONS.read_text(encoding="utf-8")

    assert 'name  = "priorityClassName"' in addons
    assert 'value = "system-cluster-critical"' in addons
