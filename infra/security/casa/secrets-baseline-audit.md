# Detect-Secrets Baseline Audit

Status: reviewed for live-secret exposure
Last reviewed: 2026-07-05
Baseline: `.secrets.baseline`
Latest verified run: `infra/security/casa/evidence/runs/20260705-133010`

This audit tracks the 120 existing detect-secrets candidates. The baseline blocks new candidate secrets. The existing entries have been reviewed for live-secret exposure; no live production secret value was identified.

## Current Summary

| Group | Count | Initial assessment |
| --- | ---: | --- |
| Tests | 54 | Reviewed: test fixtures, fake provider credentials, fake DB URLs, signing-key fixtures, and expected redaction payloads. No live secret found. |
| Migrations | 22 | Reviewed: Alembic revision IDs and template `driver://user:pass@localhost/dbname` URLs. No live secret found. |
| Docs | 19 | Reviewed: API examples and historical implementation plans with placeholders/truncated examples. No live secret found; sanitize before external sharing. |
| Source | 12 | Reviewed: auth/error-message strings and generated-key prefixes. No live secret found. |
| Charts | 7 | Reviewed: Kubernetes/TLS secret resource names, not secret values. No live secret found. |
| Infra | 4 | Reviewed: Google project-number Key Vault secret name and AKS secret rotation interval. No live secret found. |
| Scripts | 2 | Reviewed: placeholder load-test password and Key Vault secret name. No live secret found. |

Detector types:

| Detector | Count |
| --- | ---: |
| Secret Keyword | 84 |
| Hex High Entropy String | 22 |
| Basic Auth Credentials | 12 |
| Base64 High Entropy String | 2 |

## Production-Sensitive Review

Reviewed all candidates under `charts`, `infra`, `scripts`, and `src` because these are most likely to affect production runtime or deployment.

| Area | Candidates | Review result |
| --- | ---: | --- |
| Helm charts | 7 | Secret resource names such as TLS secret names and application secret object names. No secret value. |
| Infra manifests/Terraform | 4 | Secret name references and AKS rotation interval. No secret value. |
| Scripts | 2 | Placeholder password in docs and generated Key Vault secret name. No committed secret value. |
| Source | 12 | Auth message constants and key-prefix constants. No committed secret value. |

Result: no live production secret value was identified in the production-sensitive baseline review.

## Remaining Groups Review

Reviewed all candidates under `docs`, `tests`, and `migrations`.

| Area | Candidates | Review result |
| --- | ---: | --- |
| Tests | 54 | Test-only credentials and fixture strings used to exercise auth, config, webhook, provider, and redaction behavior. No live secret value. |
| Migrations | 22 | Alembic revision/down-revision identifiers and default Alembic template URLs. No live secret value. |
| Docs | 19 | Example payloads, placeholders, historical planning snippets, and truncated illustrative secrets. No live secret value. Do not send raw internal planning docs as assessor evidence without redaction review. |

Result: no live secret value was identified in the remaining baseline review.

## Open Follow-Up

1. Keep `.secrets.baseline` committed and require the baseline check before production release evidence is collected.
2. Before sharing documentation externally, export only the CASA evidence package and sanitized docs. Do not share raw `docs/superpowers/plans` content with an assessor unless separately reviewed.
3. If any future real secret is found, rotate it in the source system and remove it from git history if required by security policy.

## Current CASA Position

The new-secret control is active:

- Baseline findings: 120
- Current findings: 120
- New findings: 0
- Evidence: `infra/security/casa/evidence/runs/20260705-133010/03-detect-secrets-baseline-check.txt`

This is enough to show a repeatable control that prevents new committed credential leaks and that the existing baseline was reviewed for live-secret exposure.
