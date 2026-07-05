# Google CASA Production Readiness

Status: working evidence package
Owner: security/compliance
Environment: production

This folder is the central CASA Tier 2 readiness package for CopilotGTM production. Keep the repeatable process here; keep generated scan output out of git because it can contain sensitive paths, dependency details, or secret findings.

The repository root `.secrets.baseline` is the committed detect-secrets baseline. It records hashed current findings so new credential leaks can be detected without committing raw secret values.

## Official Basis

CASA Tier 2 requires the application owner to scan the application, remediate failed mapped CWEs, and provide evidence to an authorized assessor. The assessor submission needs the CASA Tier 2 notification email, applicable certifications, AST configuration files, and AST scan results in plain-text `.txt` format.

References:

- Tier 2 process: https://appdefensealliance.dev/casa/tier-2/tier2-overview
- CASA requirements: https://appdefensealliance.dev/casa/casa-requirements
- Accepted frameworks: https://appdefensealliance.dev/casa/tier-2/accepted-frameworks

## Production Scope

Complete this before sending evidence to an assessor.

| Item | Production value |
| --- | --- |
| Google OAuth app name | TODO |
| Google OAuth client ID | TODO |
| CASA / Google notification email | TODO |
| Authorized assessor path | TODO |
| Production API URL | `https://api.copilotgtm.com` |
| Production app URL | `https://app.copilotgtm.com` |
| Production admin URL | `https://admin.copilotgtm.com` |
| OAuth redirect URI | `https://api.copilotgtm.com/users/integrations/copilot-oauth/callback` |
| Google APIs in scope | Repo-derived in `google-oauth-scope-inventory.md`; console confirmation still required |
| Google OAuth scopes requested | Repo-derived in `google-oauth-scope-inventory.md`; console confirmation still required |
| Assessor test account | TODO |
| Data types accessed | Repo-derived in `google-oauth-scope-inventory.md`; console confirmation still required |

Copy `casa-scope-values.example.json` to `casa-scope-values.local.json` and fill the Google Console, CASA notification, assessor, and test-account values locally. The local file is ignored by git and is required by the submission preflight.

## Evidence Layout

Generated files should follow this layout:

```text
infra/security/casa/evidence/
  pipeline-runs/
    <prod-backend-Build.BuildId>/
      security-report/
      secrets-scan-reports/
      python-sca-reports/
      trivy-reports/
  infra-pipeline-runs/
    <prod-infra-Build.BuildId>/
      terraform-iac-scan/
  dast-runs/
    YYYYMMDD-HHMMSS/
      dast-manifest.txt
      08-dast-*.txt
  runs/
    YYYYMMDD-HHMMSS/
      00-scope.txt
      01-ruff-security.txt
      02-bandit-sast.txt
      03-detect-secrets.txt
      03-detect-secrets-baseline-check.txt
      03-detect-secrets-baseline-summary.txt
      04-terraform-prod-validate.txt
      05-terraform-prod-fmt-check.txt
      06-iac-trivy-config.txt
      06-python-dependency-tree.txt
      06-python-requirements.txt
      06-pip-audit-status.txt
      07-trivy-*.txt
      08-dast-*.txt
      configs/
        azure-backend-pipeline-prod.yml
        Dockerfile
        pyproject.toml
        uv.lock
        terraform-prod-main.tf
        terraform-prod-variables.tf
        terraform-prod-tfvars.txt
```

The `evidence/` folder is intentionally ignored except for its `.gitignore`. Store final assessor uploads in the secure compliance repository or assessor portal, not in this application repo.

## Readiness Command

From repo root:

```powershell
powershell -ExecutionPolicy Bypass -File infra/security/casa/run-readiness-scans.ps1
```

Optional production checks:

```powershell
powershell -ExecutionPolicy Bypass -File infra/security/casa/run-readiness-scans.ps1 `
  -RunPipAudit `
  -RunIacScan `
  -RunTrivy `
  -ImageRefs copilotgtmacrprod.azurecr.io/copilot-backend:<release-tag> `
  -RunDast `
  -ProdApiBaseUrl https://api.copilotgtm.com `
  -ProdAppUrl https://app.copilotgtm.com
```

Run DAST only during an approved production testing window. The default command avoids DAST, pip-audit, and image pulls.

## Prod Pipeline Artifact Export

Commit and push the CASA pipeline changes before running production evidence export. The backend artifacts require the updated `azure-backend-pipeline-prod.yml` on the `release/backend/*` branch, and the infra artifact requires the updated Terraform stage template on the `release/infra/*` branch. Existing prod runs that predate these changes will not contain the required CASA artifacts.

Before committing from a mixed worktree, check the required CASA publication file scope:

```powershell
uv run python scripts/security/check_casa_publication_scope.py --print-files --print-git-add --print-release-commands
```

To enforce that the current worktree contains only CASA publication changes before committing:

```powershell
uv run python scripts/security/check_casa_publication_scope.py --print-worktree-scope --fail-on-non-casa-changes
```

After staging, verify the index itself contains only CASA publication files:

```powershell
uv run python scripts/security/check_casa_publication_scope.py --fail-on-non-casa-staged
```

Before running the prod pipelines, verify the release branches contain the required CASA artifact publishing:

```powershell
uv run python scripts/security/check_casa_release_publication.py `
  --backend-ref origin/release/backend/<release> `
  --infra-ref origin/release/infra/<release>
```

Without `--backend-ref` and `--infra-ref`, the command checks the current working tree.

## Current Position

Last updated: 2026-07-05 09:54 UTC.

| Item | Current state |
| --- | --- |
| CASA commit | `61d696b0c9bfe3a1f2014cee46ad3195b197f8f4` (`Prepare CASA production evidence workflow`) |
| Active working branch | `codex/casa-production-evidence-workflow`; `main` has been moved back to `origin/main` |
| Backend release branch | `release/backend/2026-07-05-casa` at `61d696b0c9bfe3a1f2014cee46ad3195b197f8f4`; release publication check passed |
| Infra release branch | `release/infra/2026-07-05-casa` at `61d696b0c9bfe3a1f2014cee46ad3195b197f8f4`; release publication check passed |
| Backend prod pipeline | Queued manually as Azure DevOps run `1760` / build `20260705.2`; status was `notStarted` at last check |
| Infra prod pipeline | Azure DevOps run `1758` / build `20260705.1` failed in `Terraform Init, Validate & Plan` during the new Trivy IaC scan setup; Terraform init and validate passed |
| Current fix in progress | Install Trivy into `$(Agent.TempDirectory)/trivy-bin` instead of `/usr/local/bin`, then republish the release branches and rerun prod pipelines |
| Artifact export | Not ready until backend and infra prod runs complete successfully |
| Submission preflight | Fails until Google/CASA local values, prod backend artifacts, prod infra IaC artifact, and DAST evidence exist |

Resume from here by committing the Trivy install-path fix on this branch, pushing it to the backend and infra release branches, and rerunning or letting the prod pipelines rerun. After both prod runs succeed, run:

```powershell
powershell -ExecutionPolicy Bypass -File infra/security/casa/export-prod-casa-evidence.ps1 `
  -UseLatestSuccessfulRuns
```

After the prod backend pipeline finishes for the release branch, export the CASA evidence artifacts by `Build.BuildId`:

```powershell
powershell -ExecutionPolicy Bypass -File infra/security/casa/export-prod-pipeline-artifacts.ps1 `
  -RunId <Build.BuildId>
```

This downloads `secrets-scan-reports`, `python-sca-reports`, `trivy-reports`, and `security-report` into `infra/security/casa/evidence/pipeline-runs/<Build.BuildId>/` and writes `manifest.txt`.

After the prod infra pipeline finishes for the release branch, export its `terraform-iac-scan` artifact from the matching infra `Build.BuildId`:

```powershell
powershell -ExecutionPolicy Bypass -File infra/security/casa/export-prod-infra-pipeline-artifacts.ps1 `
  -RunId <Infra Build.BuildId>
```

To export both production pipeline runs and immediately run submission preflight:

```powershell
powershell -ExecutionPolicy Bypass -File infra/security/casa/export-prod-casa-evidence.ps1 `
  -BackendRunId <Backend Build.BuildId> `
  -InfraRunId <Infra Build.BuildId>
```

Or let the script select the latest successful prod backend and infra runs that contain the required CASA artifacts:

```powershell
powershell -ExecutionPolicy Bypass -File infra/security/casa/export-prod-casa-evidence.ps1 `
  -UseLatestSuccessfulRuns
```

## Approved-Window DAST Export

Run production DAST only in an approved testing window:

```powershell
powershell -ExecutionPolicy Bypass -File infra/security/casa/run-approved-window-dast.ps1 `
  -ApprovedWindow `
  -RunZap
```

This writes health endpoint DAST reports and, when `-RunZap` is set, OWASP ZAP baseline output into `infra/security/casa/evidence/dast-runs/YYYYMMDD-HHMMSS/`.

## Submission Preflight

Before sending evidence to the assessor, run:

```powershell
uv run python scripts/security/check_casa_submission_preflight.py `
  --output infra/security/casa/evidence/submission-preflight.txt
```

The preflight checks Google OAuth inventory TODOs, the latest local readiness run, exported prod pipeline artifacts, and approved-window DAST evidence.

## Submission Package

After the submission preflight passes, build the assessor package:

```powershell
uv run python scripts/security/build_casa_submission_package.py
```

The package is written under `infra/security/casa/evidence/submission-packages/YYYYMMDD-HHMMSS/`. The builder copies only the CASA root docs and generated evidence folders; it does not copy unrelated internal docs.

## Scan Matrix

| Evidence area | Command source | Output |
| --- | --- | --- |
| SAST | `uv run bandit -c pyproject.toml -r src/ -f txt`; prod pipeline artifact `security-report` | `02-bandit-sast.txt`; pipeline `bandit-report.txt` / `bandit-report.json` |
| Lint/security rules | `uv run ruff check src/ tests/` | `01-ruff-security.txt` |
| Secrets scan snapshot | `uv run detect-secrets scan` | `03-detect-secrets.txt` |
| New secret enforcement | `check_detect_secrets_baseline.py` compares the current detect-secrets JSON snapshot with `.secrets.baseline`; prod pipeline publishes `secrets-scan-reports` | `03-detect-secrets-baseline-check.txt`; pipeline `detect-secrets-baseline-check.txt` |
| Secrets baseline audit summary | PowerShell summary from `.secrets.baseline` | `03-detect-secrets-baseline-summary.txt` |
| Terraform validation | `terraform -chdir=infra/terraform/azure/prod validate` | `04-terraform-prod-validate.txt` |
| Terraform format gate | `terraform fmt -check -recursive infra/terraform/azure` | `05-terraform-prod-fmt-check.txt` |
| Terraform IaC security scan | Prod infra pipeline artifact `terraform-iac-scan`, or `run-readiness-scans.ps1 -RunIacScan` with local Trivy or Docker fallback | pipeline `terraform-iac-trivy-report.txt`; local `06-iac-trivy-config.txt` |
| Python dependency inventory | `uv tree --locked --no-dev --universal`; `uv export --format requirements.txt --locked --no-dev --no-emit-project --no-header` | `06-python-dependency-tree.txt`; `06-python-requirements.txt` |
| Python dependency SCA | Prod pipeline artifact `python-sca-reports`, or `pip-audit` when installed and `-RunPipAudit` is set | `06-pip-audit-status.txt`; pipeline `pip-audit-report.txt` |
| Container SCA | Prod pipeline artifact `trivy-reports`, or local Trivy with image refs | `07-trivy-*.txt`; pipeline `trivy-*.txt` / `trivy-*.json` |
| DAST smoke check | `run-approved-window-dast.ps1 -ApprovedWindow` during an approved production window | `08-dast-health-security*.txt/json` |
| DAST baseline | `run-approved-window-dast.ps1 -ApprovedWindow -RunZap` or assessor-approved equivalent | `08-dast-zap-baseline.txt` plus exported ZAP results |

## Production Next Steps

1. Confirm `google-oauth-scope-inventory.md` against Google Cloud Console, then fill OAuth app name, client ID, notification email, assessor path, and test account.
2. Run `check_casa_publication_scope.py --print-files --print-git-add --print-release-commands`, then optionally run `check_casa_publication_scope.py --print-worktree-scope --fail-on-non-casa-changes` if the worktree should contain no unrelated WIP. After staging, run `check_casa_publication_scope.py --fail-on-non-casa-staged`. Commit and push only the CASA publication files to the release branches used by the prod backend and infra pipelines.
3. Run `check_casa_release_publication.py --backend-ref origin/release/backend/<release> --infra-ref origin/release/infra/<release>` to verify the release branches have the CASA artifact publishing.
4. Run the updated prod backend and prod infra pipelines for the release branch.
5. Run `export-prod-casa-evidence.ps1 -UseLatestSuccessfulRuns` to export backend and infra evidence and run preflight.
6. Run `run-approved-window-dast.ps1 -ApprovedWindow -RunZap` in an approved window and attach plain-text results, then rerun preflight.
7. Run `build_casa_submission_package.py` after preflight passes.
8. Remediate high/medium CASA-mapped findings, then rerun the affected scans.
9. Submit notification email, AST configs, plain-text AST results, remediation evidence, and any SOC 2 or framework artifacts to the authorized assessor.
