import json

import pytest
from scripts.security import check_detect_secrets_baseline as checker


def _scan(results):
    return {
        "version": "1.5.0",
        "plugins_used": [],
        "filters_used": [],
        "results": results,
    }


def test_baseline_check_passes_when_current_findings_match(tmp_path):
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    report = tmp_path / "report.txt"
    payload = _scan(
        {
            "src/app.py": [
                {
                    "type": "Secret Keyword",
                    "filename": "src/app.py",
                    "hashed_secret": "abc123",
                    "is_verified": False,
                    "line_number": 12,
                }
            ]
        }
    )
    baseline.write_text(json.dumps(payload), encoding="utf-8")
    current.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = checker.run_baseline_check(
        baseline_path=baseline,
        current_scan_path=current,
        output_path=report,
    )

    assert exit_code == 0
    assert "New findings: 0" in report.read_text(encoding="utf-8")


def test_baseline_check_fails_when_current_scan_has_new_findings(tmp_path):
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    report = tmp_path / "report.txt"
    baseline.write_text(json.dumps(_scan({})), encoding="utf-8")
    current.write_text(
        json.dumps(
            _scan(
                {
                    "src/app.py": [
                        {
                            "type": "Secret Keyword",
                            "filename": "src/app.py",
                            "hashed_secret": "abc123",
                            "is_verified": False,
                            "line_number": 12,
                        }
                    ]
                }
            )
        ),
        encoding="utf-8",
    )

    exit_code = checker.run_baseline_check(
        baseline_path=baseline,
        current_scan_path=current,
        output_path=report,
    )

    text = report.read_text(encoding="utf-8")
    assert exit_code == 1
    assert "New findings: 1" in text
    assert "src/app.py|12|Secret Keyword|abc123" in text


def test_baseline_check_reads_json_from_casa_output_file(tmp_path):
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.txt"
    payload = _scan({"src/app.py": []})
    baseline.write_text(json.dumps(payload), encoding="utf-8")
    current.write_text(
        "### Detect-secrets scan\nStartedAt=2026-07-05T00:00:00Z\n\n"
        + json.dumps(payload)
        + "\nExitCode=0\n",
        encoding="utf-8",
    )

    assert checker.run_baseline_check(baseline_path=baseline, current_scan_path=current) == 0


def test_baseline_check_errors_when_scan_json_is_missing(tmp_path):
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.txt"
    baseline.write_text(json.dumps(_scan({})), encoding="utf-8")
    current.write_text("not json", encoding="utf-8")

    with pytest.raises(ValueError, match="detect-secrets JSON payload not found"):
        checker.run_baseline_check(baseline_path=baseline, current_scan_path=current)
