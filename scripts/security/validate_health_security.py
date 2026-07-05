#!/usr/bin/env python3
"""Validate public and internal health endpoint exposure.

This is a lightweight DAST smoke check for CASA evidence. It intentionally
limits itself to GET requests against known health endpoints so it can be run
in an approved production validation window without crawling the application.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import requests

SENSITIVE_TERMS = (
    "secret",
    "password",
    "token",
    "key",
    "connection",
    "database",
)

SECURITY_HEADERS = (
    "strict-transport-security",
    "x-content-type-options",
    "x-frame-options",
    "content-security-policy",
    "referrer-policy",
)

INTERNAL_ENDPOINTS = (
    "/internal/healthcheck",
    "/internal/health/detailed",
    "/internal/health/secrets",
)


def _headers_to_lower(response: requests.Response) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in response.headers.items()}


def _find_sensitive_terms(payload: Any) -> list[str]:
    payload_text = json.dumps(payload, default=str).lower()
    return [term for term in SENSITIVE_TERMS if term in payload_text]


def _missing_security_headers(response: requests.Response) -> list[str]:
    headers = _headers_to_lower(response)
    return [header for header in SECURITY_HEADERS if header not in headers]


def test_public_health_endpoint(base_url: str, timeout: float) -> dict[str, Any]:
    """Validate that the public health endpoint is reachable and low-detail."""
    endpoint = "/health"
    url = f"{base_url.rstrip('/')}{endpoint}"
    try:
        response = requests.get(url, timeout=timeout)
        try:
            data: Any = response.json()
        except ValueError:
            data = response.text

        sensitive_terms = _find_sensitive_terms(data)
        result: dict[str, Any] = {
            "endpoint": endpoint,
            "url": url,
            "status_code": response.status_code,
            "accessible": response.status_code == 200,
            "sensitive_terms": sensitive_terms,
            "missing_security_headers": _missing_security_headers(response),
            "response_sample": data,
        }
        result["passed"] = response.status_code == 200 and not sensitive_terms
        return result
    except Exception as exc:
        return {
            "endpoint": endpoint,
            "url": url,
            "accessible": False,
            "passed": False,
            "error": str(exc),
        }


def test_internal_health_endpoints(base_url: str, timeout: float) -> list[dict[str, Any]]:
    """Validate that internal health endpoints reject unauthenticated requests."""
    results: list[dict[str, Any]] = []
    for endpoint in INTERNAL_ENDPOINTS:
        url = f"{base_url.rstrip('/')}{endpoint}"
        try:
            response = requests.get(url, timeout=timeout)
            results.append(
                {
                    "endpoint": endpoint,
                    "url": url,
                    "status_code": response.status_code,
                    "properly_restricted": response.status_code in {401, 403},
                    "passed": response.status_code in {401, 403},
                }
            )
        except Exception as exc:
            results.append(
                {
                    "endpoint": endpoint,
                    "url": url,
                    "properly_restricted": False,
                    "passed": False,
                    "error": str(exc),
                }
            )
    return results


def run_validation(base_url: str, timeout: float) -> dict[str, Any]:
    public_result = test_public_health_endpoint(base_url, timeout)
    internal_results = test_internal_health_endpoints(base_url, timeout)
    passed = bool(public_result.get("passed")) and all(
        bool(result.get("passed")) for result in internal_results
    )
    return {
        "base_url": base_url.rstrip("/"),
        "passed": passed,
        "public_health": public_result,
        "internal_health": internal_results,
    }


def format_text_report(report: dict[str, Any]) -> str:
    lines = [
        "Health endpoint DAST smoke check",
        f"Base URL: {report['base_url']}",
        f"Overall: {'PASS' if report['passed'] else 'FAIL'}",
        "",
        "Public endpoint:",
    ]

    public = report["public_health"]
    lines.extend(
        [
            f"- endpoint: {public.get('endpoint')}",
            f"- status_code: {public.get('status_code', 'n/a')}",
            f"- accessible: {public.get('accessible')}",
            f"- sensitive_terms: {', '.join(public.get('sensitive_terms') or []) or 'none'}",
            "- missing_security_headers: "
            + (", ".join(public.get("missing_security_headers") or []) or "none"),
        ]
    )
    if public.get("error"):
        lines.append(f"- error: {public['error']}")

    lines.extend(["", "Internal endpoints:"])
    for result in report["internal_health"]:
        lines.append(
            "- {endpoint}: status_code={status_code}, properly_restricted={restricted}".format(
                endpoint=result.get("endpoint"),
                status_code=result.get("status_code", "n/a"),
                restricted=result.get("properly_restricted"),
            )
        )
        if result.get("error"):
            lines.append(f"  error: {result['error']}")

    lines.append("")
    lines.append(
        "Note: Missing browser security headers are informational for this health endpoint "
        "smoke check unless the assessor requires them on API responses."
    )
    return "\n".join(lines) + "\n"


def _write_if_requested(path: str | None, content: str) -> None:
    if not path:
        return
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=os.getenv("API_BASE_URL", "https://devapi.copilotgtm.com"),
        help="API base URL to validate. Defaults to API_BASE_URL or dev API.",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--json-output", help="Optional path for JSON report output.")
    parser.add_argument("--text-output", help="Optional path for text report output.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = run_validation(args.base_url, args.timeout)
    text_report = format_text_report(report)

    print(text_report, end="")
    _write_if_requested(args.text_output, text_report)
    _write_if_requested(args.json_output, json.dumps(report, indent=2, sort_keys=True) + "\n")

    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
