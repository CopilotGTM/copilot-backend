# Security Validation Scripts

This folder contains security validation and compliance checking tools.

## Scripts

### `validate_health_security.py`
**Purpose**: Security validation for health check endpoints
**Usage**:
```bash
python security/validate_health_security.py
API_BASE_URL=https://your-api.com python security/validate_health_security.py
```

**What it validates**:
- ✅ Public health endpoints don't leak sensitive data
- ✅ Internal health endpoints are properly secured
- ✅ Authorization is required for restricted endpoints
- ✅ No secrets/tokens exposed in responses

**Required for**:
- ✅ Pre-deployment security checks
- ✅ Security audits
- ✅ Compliance validation
- ✅ Penetration testing

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
