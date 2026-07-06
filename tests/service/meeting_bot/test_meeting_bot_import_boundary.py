"""Import-boundary tests for meeting bot helpers used by slim worker images."""

from __future__ import annotations

import importlib
import sys
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec

import pytest


class _BlockedAzureStorageFinder(MetaPathFinder):
    def find_spec(
        self,
        fullname: str,
        path: object | None,
        target: object | None = None,
    ) -> ModuleSpec | None:
        if fullname == "azure.storage" or fullname.startswith("azure.storage."):
            raise ModuleNotFoundError("No module named 'azure.storage'")
        return None


def test_recording_window_import_does_not_require_azure_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calendar sync imports this helper inside the integrations-worker image."""
    for module_name in list(sys.modules):
        if module_name == "src.service.meeting_bot" or module_name.startswith(
            "src.service.meeting_bot."
        ):
            monkeypatch.delitem(sys.modules, module_name, raising=False)

    finder = _BlockedAzureStorageFinder()
    sys.meta_path.insert(0, finder)
    try:
        module = importlib.import_module("src.service.meeting_bot.recording_window")
    finally:
        sys.meta_path.remove(finder)

    assert callable(module.normalize_utc)


def test_integrations_worker_bootstrap_does_not_require_azure_storage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The integrations-worker image does not install azure-storage-blob."""
    for module_name in list(sys.modules):
        if module_name.startswith("src.workers.integrations_worker") or module_name.startswith(
            "src.service.meeting_bot"
        ):
            monkeypatch.delitem(sys.modules, module_name, raising=False)

    finder = _BlockedAzureStorageFinder()
    sys.meta_path.insert(0, finder)
    try:
        module = importlib.import_module("src.workers.integrations_worker.celery_app")
    finally:
        sys.meta_path.remove(finder)

    assert module.app.main == "integrations_worker"
