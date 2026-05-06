"""Architecture guardrails for API/worker isolation in production images.

Worker and agent runtime images do not package the API layer, so this test fails
early if any code path imported from `src.api` slips into those packages.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKED_ROOTS = (
    REPO_ROOT / "src" / "agents",
    REPO_ROOT / "src" / "workers",
)


def _imports_api(node: ast.AST) -> bool:
    """Detect import statements that pull in `src.api` directly or by prefix."""
    if isinstance(node, ast.Import):
        return any(
            alias.name == "src.api" or alias.name.startswith("src.api.") for alias in node.names
        )
    if isinstance(node, ast.ImportFrom):
        return node.module == "src.api" or bool(node.module and node.module.startswith("src.api."))
    return False


def test_worker_runtime_packages_do_not_import_api_layer() -> None:
    """Workers and agent packages are copied without src/api in production images."""
    offenders: list[str] = []

    for root in CHECKED_ROOTS:
        # Scan each module and fail with a full path list so packaging mistakes
        # are easy to fix before deployment.
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            if any(_imports_api(node) for node in ast.walk(tree)):
                offenders.append(str(path.relative_to(REPO_ROOT)))

    assert offenders == []
