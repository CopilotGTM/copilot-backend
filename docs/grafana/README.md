# Grafana Dashboards

Importable Grafana dashboard JSON files for production observability.

## Import Instructions

1. Open Grafana > Dashboards > Import
2. Upload the JSON file or paste its contents
3. Select your Loki data source when prompted for `DS_LOKI`
4. Save

## Dashboards

| File | Purpose | Default Time Range |
|------|---------|--------------------|
| `dashboards/platform-overview.json` | Landing dashboard: request volume, error rates, latency, active orgs | 1h |
| `dashboards/error-investigation.json` | Error drill-down: failures by source, endpoint, worker, org | 6h |

## Variables

All dashboards use `service_name` as the primary stream selector (OTEL label).

| Variable | Default | Notes |
|----------|---------|-------|
| `service_name` | `.+` | Regex match on Loki `service_name` stream label |
| `level` | `warning\|error\|critical` | Only on Platform Overview live stream |

## Query Conventions

- Stream selector: `{service_name=~"$service_name"}`
- Level filtering: `detected_level` as a Loki stream label inside `{}`
- Structured metadata fields (from OTEL): filter with `| field="value"` after stream selector
- Status codes: use regex patterns (`4..`, `5..`) not numeric comparison
- No `| json` needed — logs are OTEL-structured, not JSON lines

## Related Documentation

- [Dashboard Plan](../Technical%20Documentation/grafana-dashboard-plan.md)
- [Logging Remediation Plan](../Technical%20Documentation/logging-remediation-plan.md)
- [Investigation Queries](../Technical%20Documentation/logging-investigation-queries.md)
