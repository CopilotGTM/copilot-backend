# Frontend: Bot Auth Page (`/bots/auth`)

## Overview

Build a single page that links a user's external bot identity (e.g. Microsoft Teams) to their CopilotGTM account. The backend handles all identity mapping and token validation — the frontend just needs to orchestrate the login check and one API call.

**Route:** `/bots/auth`

**Full URL the bot sends users to:**
```
https://app.copilotgtm.com/bots/auth?state=<token>&provider=teams
```

---

## End-to-End Flow

```
1. User messages the bot in Teams
2. Bot replies with a sign-in link pointing to /bots/auth?state=xyz&provider=teams
3. User clicks link -> browser opens this page
4. Page validates the state token (GET /users/bots/auth/validate-state)
5. Page checks if user has an active Supabase session
   a. YES -> grab access_token, call callback immediately
   b. NO  -> show login form, after login grab access_token, call callback
6. Page calls POST /users/bots/auth/callback { access_token, state }
7. Backend links the bot identity to the CopilotGTM user
8. Page shows success message: "You can close this window and return to Teams."
```

---

## Page Logic (Step by Step)

### 1. Read URL Parameters

```
state    = URL query param "state"    (required)
provider = URL query param "provider" (required, e.g. "teams")
```

If either is missing, show an error: **"Invalid link. Please go back to the bot and type `login` to get a new link."**

### 2. Validate State Token

Before doing anything else, check that the token hasn't expired.

**Request:**
```
GET /users/bots/auth/validate-state?state=<token>
```

**Response (token valid):**
```json
{
  "valid": true,
  "provider": "teams",
  "expires_in_seconds": 245
}
```

**Response (token expired/invalid):**
```json
{
  "valid": false,
  "provider": null,
  "expires_in_seconds": 0
}
```

- If `valid` is `false` -> show the **"Link Expired"** state (see UI States below). Stop here.
- If `valid` is `true` -> proceed to step 3.

### 3. Check Supabase Session

Use your existing Supabase auth to check if the user is already logged in.

- **Already logged in:** Get the `access_token` from the Supabase session and go straight to step 4 (show a spinner while this happens).
- **Not logged in:** Show the standard CopilotGTM login form. After successful login, get the `access_token` and proceed to step 4.

### 4. Call the Callback Endpoint

**Request:**
```
POST /users/bots/auth/callback
Content-Type: application/json

{
  "access_token": "<supabase_jwt_token>",
  "state": "<state_token_from_url>"
}
```

**Success Response (200):**
```json
{
  "success": true,
  "message": "Account linked successfully",
  "user_email": "user@company.com",
  "provider": "teams"
}
```

On success, show the **"Success"** state.

**Error Responses:**

| Status | Meaning | What to show the user |
|--------|---------|----------------------|
| `400` | State token invalid or expired | "This link has expired. Please go back to Teams and type **login** to get a new link." |
| `401` | Supabase access token invalid | "Your session has expired. Please log in again." (reset to login form) |
| `403` | User account is not active | "Your account is not active. Please contact your administrator." |
| `404` | No CopilotGTM account found | "No CopilotGTM account was found for your email. Please contact your administrator." |
| `500` | Server error | "Something went wrong. Please try again." |

---

## UI States

| State | What to show |
|-------|-------------|
| **Loading** | Spinner with text: "Verifying link..." (while calling validate-state) |
| **Link Expired** | Error message: "This link has expired. Please go back to Teams and type **login** to get a new link." |
| **Invalid Link** | Error message: "Invalid link. Please go back to the bot and type **login** to get a new link." |
| **Not Logged In** | Standard CopilotGTM login form. Preserve `state` and `provider` params through the login flow. |
| **Linking** | Spinner with text: "Linking your account..." (while calling callback) |
| **Success** | Green check / success card: "Your account has been linked! You can close this window and return to Teams." |
| **Error (4xx/5xx)** | Appropriate error message from the table above. |

### State Flow Diagram

```
Page Load
  |
  v
[Loading] -- validate-state --> valid?
  |                               |
  | No                            | Yes
  v                               v
[Link Expired]              Check Supabase session
                                  |
                      +-----------+-----------+
                      |                       |
                  Logged in              Not logged in
                      |                       |
                      v                       v
                  [Linking]             [Login Form]
                      |                       |
                      |                   User logs in
                      |                       |
                      |                       v
                      |                   [Linking]
                      |                       |
                      +----------+------------+
                                 |
                           callback result
                                 |
                      +----------+----------+
                      |                     |
                   Success               Error
                      |                     |
                      v                     v
                  [Success]           [Error State]
```

---

## Important Notes

1. **No auth header needed on validate-state.** It's a public endpoint (the state token itself is the secret).

2. **No auth header needed on callback.** The Supabase `access_token` is sent in the request body, not as a Bearer token. The backend verifies it server-side.

3. **State tokens are single-use.** The callback consumes the token. If the user refreshes the page after success, validate-state will return `false`. Handle this gracefully (show success or "link expired").

4. **State tokens expire in 5 minutes.** If `expires_in_seconds` is very low (e.g. < 30), you may want to warn the user or preemptively show the expired state.

5. **Preserve query params through login.** If the user is not logged in and needs to go through the login form, make sure `state` and `provider` survive the login redirect. Either:
   - Keep them in the URL and redirect back after login
   - Store them in sessionStorage before showing the login form

6. **The `provider` param is for display purposes on the frontend.** The backend already knows the provider from the state token. But the frontend can use it to customize messaging (e.g. "Return to Teams" vs "Return to Slack").

---

## Base URL

All API endpoints are relative to the backend base URL:
```
https://api.copilotgtm.com
```

Full endpoint URLs:
- `GET  https://api.copilotgtm.com/users/bots/auth/validate-state?state=<token>`
- `POST https://api.copilotgtm.com/users/bots/auth/callback`

---

## Testing

1. Trigger the flow from the Teams bot (send any message, bot responds with sign-in link)
2. Click the link -> page should load at `/bots/auth?state=...&provider=teams`
3. If already logged into CopilotGTM -> should auto-link and show success
4. If not logged in -> should show login form -> after login -> auto-link -> success
5. Go back to Teams and send a message -> bot should recognize you
6. Test with an expired link (wait 5+ minutes) -> should show "link expired"
7. Test refreshing the success page -> should handle gracefully
