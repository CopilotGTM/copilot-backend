# Security Validation Scripts

This folder contains security validation and compliance checking tools.

## Scripts

### `validate_health_security.py`

**Purpose**: Security validation for health check endpoints
**Usage**:

```bash
python scripts/security/validate_health_security.py
python scripts/security/validate_health_security.py \
  --base-url https://api.copilotgtm.com \
  --text-output health-dast.txt \
  --json-output health-dast.json
```

**What it validates**:

- Public health endpoints do not leak sensitive data
- Internal health endpoints reject unauthenticated requests
- Authorization is required for restricted endpoints
- JSON and text outputs are available for compliance evidence

**Required for**:

- ✅ Pre-deployment security checks
- ✅ Security audits
- ✅ Compliance validation
- ✅ Penetration testing

### `check_detect_secrets_baseline.py`

**Purpose**: Compare a detect-secrets JSON scan with the committed `.secrets.baseline`
and fail when new candidate secrets appear.
**Usage**:

```bash
uv run python scripts/security/check_detect_secrets_baseline.py \
  --baseline .secrets.baseline \
  --current-json secrets-scan-reports/detect-secrets-current.json \
  --output secrets-scan-reports/detect-secrets-baseline-check.txt
```

**Required for**:

- ✅ Production pipeline secret-leak gate
- ✅ CASA secrets scan evidence
- ✅ Local readiness scan consistency

### `check_casa_submission_preflight.py`

**Purpose**: Check whether the CASA evidence package has the required inventory,
readiness run, prod pipeline artifacts, and approved-window DAST evidence.
**Usage**:

```bash
uv run python scripts/security/check_casa_submission_preflight.py \
  --output infra/security/casa/evidence/submission-preflight.txt
```

**Required for**:

- ✅ CASA submission readiness review
- ✅ Final missing-evidence report before assessor submission

### `build_casa_submission_package.py`

**Purpose**: Build a sanitized CASA assessor package after preflight passes.
**Usage**:

```bash
uv run python scripts/security/build_casa_submission_package.py
```

**Required for**:

- ✅ Final CASA assessor upload package
- ✅ Keeping unrelated internal docs out of the submission bundle

### `generate_signing_secret.py`

**Purpose**: Generate signing secrets for local env vars or Azure Key Vault entries
**Usage**:

```bash
python scripts/security/generate_signing_secret.py --target session
python scripts/security/generate_signing_secret.py --target oauth --format hex
python scripts/security/generate_signing_secret.py --target all
```

**Key Vault secret names**:

- `session-secret`
- `oauth-state-secret-key`
- `voice-realtime-config-secret`

**Application env vars**:

- `SESSION_SECRET`
- `SECRET_KEY`
- `VOICE_REALTIME_CONFIG_SECRET`

## Security Best Practices

### Before Production Deployment

1. Run all security validation scripts
2. Verify no sensitive data exposure
3. Test endpoint authorization
4. Document security findings

### Regular Security Audits

- Run weekly automated security scans
- Include in CI/CD pipeline
- Alert on security failures
- Track security metrics over time

## Access Control

- **Security Team**: Read/Execute/Modify
- **DevOps**: Read/Execute
- **Compliance**: Read only
- **Auditors**: Read only

## Integration

- CI/CD Pipeline: Run on every deployment
- Security Scanning: Automated weekly scans
- Compliance: Quarterly audit reports
- Monitoring: Alert on security failures
