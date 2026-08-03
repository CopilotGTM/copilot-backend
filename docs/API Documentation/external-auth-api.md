# External Authentication Integration

Google Workspace and Microsoft Entra use the same invite-only backend architecture. The full
frontend contract, endpoints, response codes, recovery behavior, and routing rules are in
[User Authentication API](users-auth-api.md).

## Supabase provider mapping

| UI choice | Supabase provider | Backend completion | Stored method |
|---|---|---|---|
| Continue with Google | `google` | `POST /users/auth/google/complete` | `google_workspace` |
| Continue with Microsoft | Azure/Microsoft provider configured in Supabase | `POST /users/auth/microsoft/complete` | `microsoft_entra` |

The OAuth redirect must return to the frontend. The frontend supplies the resulting Supabase
access token to the backend completion endpoint. Do not send provider subjects, tenant IDs,
emails, or authentication-method values from the browser; the backend derives them from the
remotely verified provider identity.

For accounts with multiple Supabase identities, configure the access-token hook to add the
current application-approved `authentication_method` claim. Without that signed claim, the
backend deliberately rejects an ambiguous linked OAuth session.

## Self-service signup mapping

The `/signup` UI uses the same provider sessions but a separate intent and endpoint:

| UI choice | Backend signup | Provider authority required |
|---|---|---|
| Continue with Google | `POST /users/auth/google/signup` | verified email plus matching Google Workspace `hd` |
| Continue with Microsoft | `POST /users/auth/microsoft/signup` | verified email plus non-consumer Entra tenant UUID |

The browser sends organisation profile fields, the bearer session, and `Idempotency-Key`; it never
sends a company domain, provider subject, or tenant value. The backend derives the canonical
company domain from the remotely verified provider identity. Poll
`GET /users/auth/signup-requests/{signup_request_id}` with that same provider identity until
`active`, then open onboarding. Consumer accounts and company websites that do not match the
provider-derived domain are rejected. No second email-verification message is sent because provider verification is the trust
boundary. After activation, the backend makes a best-effort welcome-email attempt; email delivery
does not control whether the already-provisioned organisation is active. See
[User Authentication API](users-auth-api.md#self-service-organisation-signup) for the full
request, response, states, and stable errors.

## Google login credential configuration

Google login has a separate configuration contract from Gmail/Calendar integrations:

| Purpose | Environment variables | Key Vault secrets |
|---|---|---|
| Google login through Supabase | `GOOGLE_LOGIN_CLIENT_ID`, `GOOGLE_LOGIN_CLIENT_SECRET` | `google-login-client-id`, `google-login-client-secret` |
| Google data integrations | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | `google-integration-client-id`, `google-integration-client-secret` |

For development and migration environments, set
`GOOGLE_LOGIN_USE_INTEGRATION_CREDENTIALS=true` to resolve the login configuration from the
existing integration pair. Dedicated login environment variables always take precedence.

Production defaults to the dedicated `google-login-*` Key Vault secrets. Put the production-only
OAuth client there and configure the Supabase Google provider with that pair. The backend does not
change the Supabase provider automatically; this avoids silently rotating a live authentication
provider during an application deployment.
