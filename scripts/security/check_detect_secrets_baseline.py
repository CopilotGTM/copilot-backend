import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _extract_json_payload(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("detect-secrets JSON payload not found")

    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError("detect-secrets JSON payload not found") from exc


def load_detect_secrets_json(path: Path) -> dict[str, Any]:
    return _extract_json_payload(path.read_text(encoding="utf-8-sig"))


def finding_keys(scan: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for filename, findings in scan.get("results", {}).items():
        for finding in findings:
            keys.add(
                "|".join(
                    [
                        filename,
                        str(finding.get("line_number", "")),
                        str(finding.get("type", "")),
                        str(finding.get("hashed_secret", "")),
                    ]
                )
            )
    return keys


def build_report(baseline_keys: set[str], current_keys: set[str]) -> tuple[str, int]:
    new_findings = sorted(current_keys - baseline_keys)
    lines = [
        f"Baseline findings: {len(baseline_keys)}",
        f"Current findings: {len(current_keys)}",
        f"New findings: {len(new_findings)}",
    ]

    if new_findings:
        lines.extend(["", "New findings not present in baseline:"])
        lines.extend(new_findings)

    return "\n".join(lines) + "\n", 1 if new_findings else 0


def run_baseline_check(
    *,
    baseline_path: Path,
    current_scan_path: Path,
    output_path: Path | None = None,
) -> int:
    baseline = load_detect_secrets_json(baseline_path)
    current = load_detect_secrets_json(current_scan_path)
    report, exit_code = build_report(
        baseline_keys=finding_keys(baseline),
        current_keys=finding_keys(current),
    )

    if output_path:
        output_path.write_text(report, encoding="utf-8")
    else:
        print(report, end="")

    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fail when detect-secrets current scan contains findings outside the committed baseline."
    )
    parser.add_argument("--baseline", type=Path, default=Path(".secrets.baseline"))
    parser.add_argument("--current-json", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    return run_baseline_check(
        baseline_path=args.baseline,
        current_scan_path=args.current_json,
        output_path=args.output,
    )


if __name__ == "__main__":
    sys.exit(main())
