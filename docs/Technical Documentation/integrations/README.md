# Integrations Technical Documentation

Documentation for CopilotGTM's integration sync system — OAuth, data sync, error handling, and provider architecture.

## Documents

| Document | Description |
|----------|-------------|
| [architecture-overview.md](architecture-overview.md) | High-level architecture, supported providers, directory structure |
| [oauth-and-token-management.md](oauth-and-token-management.md) | Custom OAuth flow, callback scope validation, token storage, refresh, and revocation detection |
| [reauth-and-permission-recovery.md](reauth-and-permission-recovery.md) | Backend reconnect model, expired-auth vs permission-drift handling, and API state contract |
| [three-tier-sync-architecture.md](three-tier-sync-architecture.md) | Celery task tiers, beat schedule, session management |
| [error-handling-and-resilience.md](error-handling-and-resilience.md) | 401 retry, 429 rate limiting, circuit breaker, timeout tracking |
| [sync-by-provider.md](sync-by-provider.md) | Per-provider sync details: calendar, email, messaging, CRM |
