"""Regression test for the one-command local E2E runner."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def test_runner_reads_key_vault_credentials_and_starts_auth_smoke(tmp_path: Path) -> None:
    """Keep Azure credential retrieval outside pytest while preserving one local command."""
    repo_root = Path(__file__).resolve().parents[2]
    runner = repo_root / "scripts" / "e2e" / "run-dev.ps1"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "uv-arguments.txt"

    (fake_bin / "az.ps1").write_text(
        """
if ($args[0] -eq "account") { exit 0 }
$secretName = $args[[Array]::IndexOf($args, "--name") + 1]
if ($secretName -eq "e2e-user-email") { Write-Output "e2e@example.com"; exit 0 }
if ($secretName -eq "e2e-user-password") { Write-Output "key-vault-password"; exit 0 }
exit 9
""".strip(),
        encoding="utf-8",
    )
    (fake_bin / "uv.ps1").write_text(
        """
if ($env:E2E_API_BASE_URL -ne "https://devapi.copilotgtm.com") { exit 11 }
if ($env:E2E_USER_EMAIL -ne "e2e@example.com") { exit 12 }
if ($env:E2E_USER_PASSWORD -ne "key-vault-password") { exit 13 }
if ($env:E2E_REQUIRE_CONFIG -ne "1") { exit 14 }
Set-Content -LiteralPath $env:E2E_TEST_MARKER -Value ($args -join " ")
exit 0
""".strip(),
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["E2E_TEST_MARKER"] = str(marker)
    pwsh = shutil.which("pwsh")
    assert pwsh is not None

    # The executable path is resolved locally and every argument is fixed by this test.
    result = subprocess.run(  # noqa: S603
        [
            pwsh,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(runner),
            "auth",
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8").strip() == (
        "run --no-project --with pytest --with httpx pytest e2e/test_auth.py -m e2e -v"
    )
