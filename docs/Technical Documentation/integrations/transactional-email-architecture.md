# Transactional Email Architecture

## Decision

Build one **transactional email module inside the existing backend**, not a separate
microservice. Product-owned messages such as signup, invitations, password reset,
task assignment, and “agent output ready” should use the same application facade,
template registry, Resend adapter, delivery ledger, and Celery worker path.

This is a layered modular-monolith design: it reuses PostgreSQL, Celery/Redis,
Resend, and the existing organisation-scoped `NotificationDelivery` idempotency
ledger. Control-plane emails need the same small delivery record in the master
database so the intent can commit atomically with signup/auth/membership state.
A dedicated `email` queue isolates provider latency and lets email capacity scale
without blocking API, organisation-provisioning, chat, or deal workers.

User-owned Gmail/Outlook sending remains in `src/service/email_sends/`; it requires
mailbox OAuth, confirmation, drafts, and threading. Transactional email must not
impersonate a user's mailbox.

## Boundaries and flow

```text
Domain transaction -> TransactionalEmailService.record(PENDING) -> commit
                                                               -> Celery `email.send`
                                                               -> template renderer
                                                               -> Resend adapter
                                                               -> SENT / FAILED
```

1. A domain service calls `TransactionalEmailService.record(...)` inside the same
   database transaction as the state that requires the email. Routers and agents
   never call Resend or render HTML.
2. The facade validates a typed request and creates or reuses a `PENDING` delivery.
   Publication happens only after the caller commits. The task contains only the
   persistence scope, `organisation_id` when applicable, and `delivery_id`.
3. The worker reloads trusted data, renders a versioned template, claims and sends
   the delivery, and records the provider ID.
4. Celery retries transient provider/network failures with exponential backoff and
   jitter. Validation, missing-recipient, and policy failures are terminal.
5. A small periodic recovery task scans both master and organisation delivery
   ledgers, republishes stale `PENDING` deliveries, and reclaims stale `SENDING`
   deliveries. This closes the database-commit/Celery-publish gap.

Acceptance means “durably queued”, not “delivered”; APIs do not wait for Resend.
Every transactional provider call occurs in the email worker.

## Application contract

Use a closed email type plus a typed payload, rather than allowing callers to pass
arbitrary HTML:

```python
await transactional_email.record(
    EmailRequest(
        email_type=EmailType.TASK_ASSIGNED,
        organisation_id=organisation_id,
        source_type="crm_task",
        source_id=task.task_id,
        recipient=EmailRecipient(user_id=assignee_id, email=assignee_email),
        payload=TaskAssignedPayload(task_id=task.task_id, is_update=False),
    )
)
```

Idempotency is `(organisation, email_type, source, recipient)`. A deliberate
repeat needs a distinct source revision or occurrence ID. The worker never accepts
tenant identity, sender, template, or provider configuration from an LLM payload.

## Layer responsibilities

- **Domain producer:** decides whether an email is required and supplies stable
  business identifiers. Signup, membership, tasks, and agent runs own this policy.
- **Application facade:** validates requests, resolves recipients under the
  organisation boundary, and records the durable intent in the caller's unit of
  work. A post-commit dispatcher publishes it.
- **Template registry:** maps `EmailType` to a versioned renderer producing subject,
  escaped HTML, and plain text. Links come from server config.
- **Delivery worker:** owns claim, retry classification, status transitions, and
  recovery. It contains no signup/task/agent business rules.
- **Provider adapter:** alone imports Resend; it applies timeouts and returns a
  provider ID or typed failure.

Suggested package boundary:

```text
src/service/transactional_email/{contracts,service,templates}.py
src/infra/email/resend_provider.py
src/workers/organization_worker/tasks/email.py
```

Organisation producers reuse `NotificationDelivery`; control-plane producers use
a matching master record. Both share one contract/state machine, avoiding a
cross-database transaction.

## Production requirements

- Do not persist passwords, reset tokens, OAuth tokens, or unrestricted agent
  output in delivery metadata. Generate short-lived links in trusted code and
  retain only the minimum template snapshot needed for deterministic retries.
- Resolve organisation and recipient authorization before creating the intent;
  escape all user-controlled template values. Keep provider credentials in Key
  Vault and suppress recipient addresses and message bodies from normal logs.
- Measure queued/sent/failed/recovered counts, queue age, provider latency, and
  retries. Correlate source, delivery, Celery, and provider IDs.
- Alert on oldest pending age, terminal-failure rate, and recovery backlog. Retain
  delivery audit metadata according to the product's privacy policy.

## Incremental rollout

1. Extract the current Resend call and shared layout from
   `src/infra/auth/email_service.py` behind the provider and template boundaries.
2. Add the facade, master ledger, `email` queue/task, and recovery coverage behind
   a feature flag.
3. Migrate signup welcome first, then invitations/tasks, then password reset and
   agent output. Test escaping, deduplication, retry, and stale recovery per type.
4. Remove direct `email_service` calls only after every caller is inventoried and
   the new path is observed in production. Keep `EmailSendService` unchanged.

This gives one production email path today while leaving room for more templates,
providers, and separately scaled workers without committing to a microservice.
