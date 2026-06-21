"""Import-boundary checks for the deal-worker light image.

The light-worker Docker image intentionally omits ``src/api``. Deal-worker task
imports must therefore keep shared DTOs and contracts outside the API package.
"""

from __future__ import annotations

import importlib
import sys
from contextlib import contextmanager
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec


class _BlockSrcApiImports(MetaPathFinder):
    def find_spec(
        self,
        fullname: str,
        path: object | None,
        target: object | None = None,
    ) -> ModuleSpec | None:
        if fullname == "src.api" or fullname.startswith("src.api."):
            raise ModuleNotFoundError("No module named 'src.api'")
        return None


@contextmanager
def _without_src_api():
    blocked = _BlockSrcApiImports()
    removed_prefixes = (
        "src.api",
        "src.service.crm.risks",
        "src.workers.deal_worker",
    )
    cached_modules = {
        name: module
        for name, module in sys.modules.items()
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in removed_prefixes)
    }
    for name in cached_modules:
        sys.modules.pop(name, None)

    sys.meta_path.insert(0, blocked)
    try:
        yield
    finally:
        sys.meta_path.remove(blocked)
        for name in list(sys.modules):
            if any(name == prefix or name.startswith(f"{prefix}.") for prefix in removed_prefixes):
                sys.modules.pop(name, None)
        sys.modules.update(cached_modules)


def test_deal_worker_tasks_import_without_api_package() -> None:
    with _without_src_api():
        importlib.import_module("src.workers.deal_worker.tasks")
