# SOC 2 and Google CASA Preparedness

Status: working tracker
Last reviewed: 2026-07-05
Scope: infrastructure and backend controls visible in this repository. This is not a formal SOC 2 audit report.

## Recommendation

Do Google CASA Tier 2 readiness first if Google app verification or Google API access is a near-term blocker.

CASA Tier 2 is smaller than SOC 2 for us because it is application-security focused: application scanning, remediation, evidence submission, assessor verification, and a Letter of Validation. SOC 2 is broader: organization-wide controls, control owners, policies, operational evidence, change management, access reviews, incident response, vendor risk, and audit-period evidence.

SOC 2 should continue as the long-running compliance program, but CASA Tier 2 is the better immediate implementation track.

## Current Implemented Controls

| Area | Current state | Repo evidence |
| --- | --- | --- |
| Prod Key Vault network isolation | Prod Key Vault private endpoints are enabled. Public network access is driven off the private endpoint flag. | `infra/terraform/azure/prod/terraform.tfvars`, `infra/terraform/azure/prod/main.tf` |
| Prod PostgreSQL network exposure | Prod PostgreSQL public network access is disabled by variable. | `infra/terraform/azure/prod/terraform.tfvars`, `infra/terraform/azure/prod/main.tf` |
| Storage public blob exposure | Nested public blob/container access is disabled. Shared Key access is disabled. TLS 1.2 is enforced. | `infra/terraform/azure/prod/main.tf`, `infra/terraform/azure/modules/storage_account/main.tf` |
| Storage recovery and audit | Blob delete retention, container delete retention, blob versioning, and Blob service diagnostic logs are configured. | `infra/terraform/azure/prod/main.tf`, `infra/terraform/azure/modules/storage_account/main.tf` |
| Storage browser CORS | CORS is limited to `GET`, `HEAD`, `PUT`, and `OPTIONS`. `POST` and `DELETE` are not allowed from the browser. | `infra/terraform/azure/modules/storage_account/main.tf`, `infra/terraform/azure/local/main.tf` |
| SAS permissions | Read SAS is read-only. Upload SAS is create/write only. No delete SAS is generated. | `src/infra/storage/azure_org_blob_storage.py`, `src/infra/storage/azure_app_blob_storage.py` |
| SAS expiry | Browser read SAS URLs are capped at 30 minutes. Upload/write SAS defaults to 15 minutes. | `src/api/routers/chat_attachments/access_handler.py`, `src/api/routers/knowledge_base/access_handler.py`, storage services |
| CI identity | Service principals are configured without generated passwords, and Azure DevOps service connections use workload identity federation. | `infra/terraform/azure/prod/main.tf`, `infra/terraform/azure/modules/service_connection/main.tf` |

## Known Gaps

| Gap | Why it matters | Next action |
| --- | --- | --- |
| Redis is still public-network reachable in prod | Redis Private Link is coded but disabled because the current Redis offering/SKU path did not support the desired private endpoint implementation. | Decide whether to upgrade/migrate Redis to an Azure offering/SKU that supports Private Link, then enable `enable_redis_private_endpoint` in prod. |
| Storage accounts are not private endpoint only | Direct browser upload/download via SAS requires the browser to reach the Blob public endpoint. Turning on Storage Private Link alone would break that flow for external users. | Keep public network enabled with strict SAS, no public containers, no Shared Key, short expiry, and limited CORS. Revisit if we move uploads through backend or another private upload architecture. |
| CASA evidence package does not exist yet | CASA validation needs scan output, configuration files, remediation evidence, and assessor submission. | Start CASA Tier 2 readiness package now. |
| SOC 2 operating evidence is not centralized | Terraform controls help, but SOC 2 also needs policy and operating evidence over time. | Create an evidence folder/process for access reviews, change approvals, incident drills, vulnerability reviews, vendor reviews, and backup/restore tests. |
| Vulnerability management cadence is not documented here | CASA and SOC 2 both need repeatable evidence that SAST/SCA/DAST/secrets/IaC findings are reviewed and remediated. | Add CI/reporting jobs or document existing tools, owners, severity SLA, and monthly evidence export. |
| Deal asset blob cleanup is partial | Deal asset delete currently soft-deletes app metadata; blob cleanup should be confirmed or implemented separately. | Add backend blob cleanup or retention-based cleanup for deal assets. |

## Google CASA Tier 2 Track

Official App Defense Alliance guidance says Tier 2 can let a developer scan the application, provide scan results and evidence to an authorized assessor, and receive a Letter of Validation without giving the assessor direct access to code or infrastructure. The same guidance says the older self-scanning process is deprecated for compliance and should be used for readiness unless the notification instructs otherwise.

CASA requirements apply across all assessment tiers. They are based on OWASP ASVS and are mapped to MITRE CWEs where applicable. To be verified, the application must pass every applicable CASA requirement; valid certifications such as SOC 2 can reduce redundant testing when accepted by the assessor.

CASA Tier 2 immediate checklist:

1. Identify the exact Google OAuth app, APIs, scopes, backend endpoints, and frontend URLs in scope.
2. Confirm whether we have a Google or App Defense Alliance Tier 2 notification email.
3. Pick the assessment path from the notification. If no notification exists, still run the same scans as readiness.
4. Run SAST, SCA/dependency scan, secrets scan, IaC scan, and DAST against the in-scope app.
5. Remediate failed CWEs that map to CASA requirements.
6. Export AST configuration files and plain text scan results.
7. Collect any valid certification or audit artifacts, especially SOC 2 if available, because it can accelerate CASA review.
8. Submit through the authorized assessor flow and track the Letter of Validation.

CASA references:

- Tier 2 process: https://appdefensealliance.dev/casa/tier-2/tier2-overview
- Accepted security frameworks: https://appdefensealliance.dev/casa/tier-2/accepted-frameworks
- CASA requirements: https://appdefensealliance.dev/casa/casa-requirements

## CASA Current-State Gap Analysis

| CASA evidence area | Current state in this repo | Gap |
| --- | --- | --- |
| In-scope Google app and data access | Google OAuth and integration code exists for Gmail, Google Calendar, and Google Chat. Prod OAuth redirect URI is `https://api.copilotgtm.com/users/integrations/copilot-oauth/callback`. Repo-derived scopes and data access are documented; `casa-scope-values.example.json` defines the ignored local values file required for console/assessor fields. | Copy the example to `casa-scope-values.local.json`, fill Google Console, notification, assessor, and test-account values, then rerun preflight. |
| SAST | `ruff` includes security rules and `bandit` is configured in `pyproject.toml`; prod/dev backend pipelines publish a Bandit JSON artifact. | CASA expects AST config and plain-text scan results. The repo does not yet have a CASA evidence folder that stores the exact commands, config files, and exported `.txt` assessor package. |
| SCA / dependency and container scanning | Prod/dev backend pipelines run Trivy against built images and publish JSON reports. The CASA runner now exports a locked Python dependency tree and requirements file from `uv.lock`. | Python vulnerability results and prod container Trivy evidence still need assessor-friendly plain-text exports for the same production build. |
| Secrets scanning | `detect-secrets` is installed in the dev dependency group. `.secrets.baseline` and `infra/security/casa/secrets-baseline-audit.md` now provide a reviewed baseline. The CASA runner and prod pipeline both block new candidate findings, and the prod pipeline publishes `secrets-scan-reports`. | After the next prod release run, export `secrets-scan-reports` with `infra/security/casa/export-prod-pipeline-artifacts.ps1 -RunId <Build.BuildId>`. |
| IaC scanning | Terraform controls exist for Key Vault Private Link, PostgreSQL private endpoint, storage hardening, workload identity federation, and Redis Private Link wiring. The prod infra pipeline now publishes `terraform-iac-scan`, and the CASA runner supports `-RunIacScan` as a local fallback. | Export the prod infra pipeline `terraform-iac-scan` artifact, or run `run-readiness-scans.ps1 -RunIacScan` from an environment with local Trivy or Docker and resolve or document findings. |
| DAST | Health endpoint DAST smoke check now emits text and JSON evidence through the CASA runner when `-RunDast` is used. `run-approved-window-dast.ps1` provides an approved-window wrapper for the smoke check and optional OWASP ZAP baseline. | No approved-window production DAST evidence was found. Run `run-approved-window-dast.ps1 -ApprovedWindow -RunZap` or assessor-approved equivalent against the CASA in-scope URLs. |
| Remediation evidence | Security-related tests and Terraform validation exist in the repo. | Findings need a dated remediation log with owner, severity, status, commit/PR, and retest evidence. |
| Infrastructure controls | Prod Key Vault and PostgreSQL are private; storage has strict SAS/CORS/public-container controls; Redis Private Link remains disabled due to current SKU/offering limitation. | Redis public network exposure remains the main infra gap, but it is secondary to assembling the CASA evidence package unless the assessor flags it directly. |
| Submission readiness | `check_casa_submission_preflight.py` checks Google OAuth inventory TODOs, the latest readiness run, prod pipeline artifacts, and approved-window DAST evidence. | Run the preflight after the prod pipeline artifact export and approved-window DAST run; resolve every failure before assessor submission. |
| Submission packaging | `build_casa_submission_package.py` builds a sanitized assessor package after preflight passes. | Build the package only after all preflight failures are resolved. |

## SOC 2 Track

SOC 2 should be handled as a control program, not only infrastructure changes.

Minimum SOC 2 workstream:

1. Define control owners for security, availability, confidentiality, privacy, change management, incident response, vendor risk, and access management.
2. Centralize evidence for production access, privileged access, quarterly access reviews, onboarding/offboarding, break-glass access, and secret rotation.
3. Centralize change evidence from pull requests, approvals, CI checks, Terraform plans, and production applies.
4. Document vulnerability management: tools, scan frequency, severity SLA, exceptions, and remediation evidence.
5. Document backup, restore, and disaster recovery testing.
6. Document incident response process and run at least one tabletop exercise.
7. Document vendor inventory and vendor risk review.
8. Keep this infra tracker updated after each control improvement.

## Prioritized Next Steps

1. Run the prod backend pipeline for the release branch, then run `infra/security/casa/export-prod-pipeline-artifacts.ps1 -RunId <Build.BuildId>` to export `secrets-scan-reports`, `python-sca-reports`, `trivy-reports`, and `security-report`.
2. Confirm Google app scope in Google Cloud Console, then fill ignored `infra/security/casa/casa-scope-values.local.json` from `casa-scope-values.example.json`.
3. Run `infra/security/casa/run-approved-window-dast.ps1 -ApprovedWindow -RunZap` against the in-scope prod URLs during an approved testing window. Convert any assessor-facing supplemental results to plain-text `.txt`.
4. Run `uv run python scripts/security/check_casa_submission_preflight.py --output infra/security/casa/evidence/submission-preflight.txt` and resolve every failure.
5. Run `uv run python scripts/security/build_casa_submission_package.py` after preflight passes.
6. Triage and remediate findings by CASA severity/CWE mapping. Track owner, fix PR/commit, exception reason if any, and retest evidence.
7. Decide the Redis path after CASA readiness is underway: either upgrade/migrate to a Redis offering/SKU that supports the required Private Link path, or document compensating controls until migration.
8. Keep storage public network enabled for direct browser SAS for now, with current controls: no public containers, no Shared Key, short-lived user-delegation SAS, and limited CORS. Revisit only if product architecture moves uploads/downloads through backend or another private path.
9. Create SOC 2 evidence folders and owners after the first CASA package exists, because CASA evidence can become the first vulnerability-management evidence set for SOC 2.

## Immediate Implementation Slice

Do this next before more Terraform changes:

1. Add `infra/security/casa/README.md` with the official CASA references, in-scope app inventory, scan command matrix, and submission checklist.
2. Add `infra/security/casa/evidence-log.md` with dated rows for scan runs, findings, remediation, and retest status.
3. Add or document local commands that produce plain-text artifacts:
   - SAST: `uv run bandit -c pyproject.toml -r src/ -f txt`
   - Secrets: `uv run detect-secrets scan`
   - Lint/security rules: `uv run ruff check src/ tests/`
   - Terraform validation: `terraform -chdir=infra/terraform/azure/prod validate`
   - DAST: OWASP ZAP baseline or the assessor-approved equivalent against the in-scope prod URLs
4. Preserve pipeline-produced Bandit and Trivy artifacts as supporting evidence, but do not rely on JSON-only output for CASA submission.

## Change Log

- 2026-07-05: Created tracker after Key Vault Private Link, storage hardening, Redis Private Link feasibility review, and SAS/CORS hardening review.
