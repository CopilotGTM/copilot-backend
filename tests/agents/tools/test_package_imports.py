from __future__ import annotations

import builtins
import importlib
import sys


def test_registry_import_does_not_require_exa_sdk(monkeypatch) -> None:
    # Exa search uses the base httpx dependency, so importing an agent registry
    # must never start requiring the provider's optional synchronous SDK.
    original_import = builtins.__import__

    def guarded_import(name, module_globals=None, module_locals=None, fromlist=(), level=0):
        if name == "exa_py":
            raise ModuleNotFoundError("No module named 'exa_py'")
        return original_import(name, module_globals, module_locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    for module_name in (
        "src.agents.tools.registry",
        "src.agents.tools.search.internet_search",
        "src.agents.tools.search",
        "src.agents.tools",
    ):
        sys.modules.pop(module_name, None)

    registry = importlib.import_module("src.agents.tools.registry")

    assert "VoiceRealtimeAgent" in registry.AGENT_TOOLS


def test_voice_realtime_prompts_import_does_not_require_agno(monkeypatch) -> None:
    original_import = builtins.__import__

    def guarded_import(name, module_globals=None, module_locals=None, fromlist=(), level=0):
        if name.startswith("agno"):
            raise ModuleNotFoundError("No module named 'agno'")
        return original_import(name, module_globals, module_locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    for module_name in (
        "src.agents.workflows.voice_realtime",
        "src.agents.workflows.voice_realtime.agent",
        "src.agents.workflows.voice_realtime.prompts",
    ):
        sys.modules.pop(module_name, None)

    prompts = importlib.import_module("src.agents.workflows.voice_realtime.prompts")

    assert hasattr(prompts, "get_voice_realtime_system_prompt")


def test_intelligence_package_import_is_lazy(monkeypatch) -> None:
    original_import = builtins.__import__

    def guarded_import(name, module_globals=None, module_locals=None, fromlist=(), level=0):
        if name.startswith("agno"):
            raise ModuleNotFoundError("No module named 'agno'")
        return original_import(name, module_globals, module_locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    for module_name in (
        "src.agents.tools.intelligence",
        "src.agents.tools.intelligence.get_compressed_transcript",
    ):
        sys.modules.pop(module_name, None)

    intelligence = importlib.import_module("src.agents.tools.intelligence")

    assert "create_get_meeting_details" in intelligence.__all__


def test_communication_package_import_is_lazy(monkeypatch) -> None:
    original_import = builtins.__import__

    def guarded_import(name, module_globals=None, module_locals=None, fromlist=(), level=0):
        if name.startswith("agno"):
            raise ModuleNotFoundError("No module named 'agno'")
        return original_import(name, module_globals, module_locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    for module_name in (
        "src.agents.tools.communication",
        "src.agents.tools.communication.chat_attachments",
    ):
        sys.modules.pop(module_name, None)

    communication = importlib.import_module("src.agents.tools.communication")

    assert "create_get_email_details" in communication.__all__
