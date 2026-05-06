"""Shared application contracts used across API, service, agent, and worker layers.

Contracts live in this namespace so runtime-boundary checks can be shared with
workers without importing `src.api` dependencies into background images.
"""
