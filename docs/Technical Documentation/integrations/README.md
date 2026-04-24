# Integrations Technical Documentation

Documentation for CopilotGTM's live integration sync system: OAuth, scheduled sync, CRM initial sync, and reconnect behavior.

Use this folder as the concise source of truth for runtime behavior. The worker README is only an implementation companion.

## Documents

| Document | Description |
|----------|-------------|
| [architecture-overview.md](architecture-overview.md) | High-level architecture, supported providers, directory structure |
| [crm-initial-sync.md](crm-initial-sync.md) | Live HubSpot/Salesforce post-connect CRM chain, metadata, and persisted tables |
| [oauth-and-token-management.md](oauth-and-token-management.md) | Custom OAuth flow, callback scope validation, token storage, refresh, and revocation detection |
| [reauth-and-permission-recovery.md](reauth-and-permission-recovery.md) | Backend reconnect model, expired-auth vs permission-drift handling, and API state contract |
| [three-tier-sync-architecture.md](three-tier-sync-architecture.md) | Celery task tiers, beat schedule, session management |
| [error-handling-and-resilience.md](error-handling-and-resilience.md) | 401 retry, 429 rate limiting, circuit breaker, timeout tracking |
| [sync-by-provider.md](sync-by-provider.md) | Per-provider sync details: calendar, email, messaging, CRM |

## Current CRM Reality

- HubSpot CRM initial sync is live through `notes`
- Salesforce CRM initial sync is live through `notes`
- both providers write tasks and notes into `crm_activities`
- scheduled CRM refresh today is schema-only fan-out; record sync is post-connect/manual
