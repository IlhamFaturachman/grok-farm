# Token Lifecycle & Refresh Strategy

End-to-end documentation of how Grok CLI / OIDC tokens flow from the farmer
through grok2api, and how they are kept alive — derived from decompiling the
official `grok.exe` binary (Rust, 132 MB) and auditing the grok2api Go source.

---

## 1. Architecture at a glance

```
  grok-farm (host)                        grok2api (docker)
  ┌──────────────────────┐  g2a_import    ┌─────────────────────────────┐
  │ Camoufox + IMAP      │  ───────────►  │ cli/import.go (parse)       │
  │ → PKCE OAuth login   │   JSON file    │ → encrypted in DB           │
  │ → {access, refresh,  │   or POST      │                             │
  │    id_token, exp}    │  /import API   │ credential_scheduler.go     │
  └──────────────────────┘                │ → proactive refresh (3 min) │
        produces tokens                   │                             │
        once per account                  │ gateway/service.go          │
                                          │ → reactive refresh on 401   │
                                          └─────────────────────────────┘
                                                 keeps tokens alive
                                                 indefinitely
```

**The farmer produces tokens once; grok2api owns their entire lifetime after
that.** No refresh logic should live in the farmer.

---

## 2. Token acquisition (farmer, once per account)

The farmer runs the full PKCE flow in a real browser (Camoufox) because
xAI's authorize endpoint requires Turnstile + a visible consent page:

```
GET  auth.x.ai/oauth2/authorize?response_type=code
                             &client_id=b1a00492-...
                             &redirect_uri=http://127.0.0.1:56121/callback
                             &scope=openid profile email offline_access
                                     grok-cli:access api:access
                             &code_challenge=<S256>
                             &code_challenge_method=S256
                             &state=<random>
                             &nonce=<random>

  ↓ browser: email login + password + Turnstile + consent
  ↓ redirect to 127.0.0.1:56121/callback?code=<auth_code>

POST auth.x.ai/oauth2/token
     grant_type=authorization_code
     client_id=b1a00492-073a-47ea-816f-4c329264a828
     code=<auth_code>
     redirect_uri=http://127.0.0.1:56121/callback
     code_verifier=<PKCE verifier>

  → { access_token, refresh_token, id_token, expires_in: 21600, scope }
```

| Field | Value | Source |
|-------|-------|--------|
| `client_id` | `b1a00492-073a-47ea-816f-4c329264a828` | Grok CLI's public client ID |
| `scope` | `openid profile email offline_access grok-cli:access api:access` | (+ `conversations:read conversations:write`) |
| `redirect_uri` | `http://127.0.0.1:56121/callback` | loopback, intercepted by Playwright route |
| `expires_in` | `21600` (6 hours) | returned by xAI |

The result is written to `results/batch_*/accounts.json`:

```json
[{
  "email": "user123@example.com",
  "password": "$ExamplePass1",
  "tokens": {
    "access_token":  "eyJ...",
    "refresh_token": "REDACTED...",
    "id_token":      "eyJ...",
    "expires_at":    "2026-07-10T07:46:28.802639Z",
    "expires_in":    21600,
    "client_id":     "b1a00492-...",
    "auth_mode":     "oidc",
    "scope":         "openid profile email offline_access ..."
  }
}]
```

---

## 3. Refresh strategy (decompiled from `grok.exe`)

The official Grok CLI binary manages tokens with a two-tier refresh strategy.
This is the canonical behavior grok2api reimplements.

### 3.1 The refresh call — no browser needed

Once a `refresh_token` exists, **every subsequent refresh is a pure HTTP POST**.
No browser, no redirect, no PKCE verifier reused. This is OAuth2 RFC 6749 §6:

```
POST https://auth.x.ai/oauth2/token
Content-Type: application/x-www-form-urlencoded
Accept: application/json

grant_type=refresh_token
&refresh_token=<current refresh_token from storage>
&client_id=b1a00492-073a-47ea-816f-4c329264a828
&scope=openid profile email offline_access grok-cli:access api:access
```

Only four fields. Public client — **no `client_secret`**, no `code_verifier`,
no `redirect_uri`. The `code`/`verifier` from login are never reused.

> The CLI discovers the token endpoint dynamically via
> `{issuer}/.well-known/openid-configuration` → `token_endpoint`.
> For first-party xAI this resolves to `https://auth.x.ai/oauth2/token`.

**Response (xAI rotates the refresh_token):**

```json
{
  "access_token":  "eyJ...",              // new bearer
  "refresh_token": "OV9u...",             // ROTATED — supersedes the old one
  "id_token":      "eyJ...",
  "expires_in":    21600,
  "scope":         "openid profile email offline_access ...",
  "token_type":    "Bearer"
}
```

### 3.2 Two refresh triggers

| Trigger | When | Action |
|---------|------|--------|
| **Proactive** | ~5 min before `expires_at` (CLI) / 3 min (grok2api) | background task refreshes before any request fails |
| **Reactive** | server returns 401/403 on an authed request | refresh → retry the request **once** |

The proactive lead time is a configurable **buffer**. From the binary:

> `# Disable the proactive buffer: refresh at expiry or on a 401 (set to 0)`
> `# Set to 0 to only refresh on 401. Set higher for very short-lived tokens.`

### 3.3 Refresh-token rotation (the critical detail)

xAI **rotates the refresh_token on each use** (RFC 6749 §10.4 / RFC 9700).
Each successful refresh invalidates the old refresh_token and issues a new one.

Evidence from the binary:
- `rotation detected`
- `sibling-rotation detected; demoting to transient`
- `rotated by external process`

**Implications:**
1. Always persist the **new** `refresh_token` from the response atomically.
2. Never reuse the old one — it's invalid; reusing it triggers `refresh_token_rejected`.
3. Concurrent processes sharing one token store **cannot** each refresh — the
   second would hit an already-rotated (rejected) token.

### 3.4 Multi-process coordination (sibling model)

The CLI coordinates concurrent processes sharing `~/.grok/auth.json` via a
refresh lock + disk adoption:

| Behavior | Log string |
|----------|------------|
| Lost refresh race → read sibling's result | `another process already refreshed, using disk token` |
| Adopt on-disk token | `picked up sibling-written token from disk` |
| Proactive short-circuit | `proactive refresh skipped, adopted sibling token from disk` |
| Rotation by sibling → don't escalate | `sibling-rotation detected; demoting to transient` |

Net: **exactly one process refreshes**; the rest adopt the disk-written result.

### 3.5 Failure state machine

```
refresh attempt
  ├─ success ──────────────► store new tokens, clear failure state
  │
  ├─ refresh_token_rejected ─► sibling rotated it?
  │     ├─ yes → demote to transient, adopt disk token
  │     └─ no  → permanent failure
  │
  ├─ transient failure ────► retry w/ backoff, track consecutive count
  │     └─ escalating consecutive transient failures to permanent
  │
  ├─ no_refresh_authority ─► no refresh_token + no external provider
  │
  └─ permanent failure ───► refresh_chain short-circuit
        ├─ skipping proactive refresh, permanent failure still set
        └─ re-authentication required (run /login)
```

The escalation (transient → permanent) is the **anti-loop guard**: it stops
the client from hammering a dead/revoked refresh_token. Once permanent, all
refresh is short-circuited until interactive re-authentication.

---

## 4. grok2api's implementation (audited)

grok2api reimplements the full strategy in Go. Source files:

| Layer | File | Role |
|-------|------|------|
| OIDC provider | `infra/provider/cli/oauth.go` | HTTP refresh call + response parsing |
| Credential service | `application/account/service.go` | Lock, singleflight, failure state |
| Scheduler | `application/account/credential_scheduler.go` | Proactive timer loop |
| Gateway | `application/gateway/service.go` | Reactive 401 refresh + retry |
| Import | `infra/provider/cli/import.go` | Parse farmer's JSON into DB |

### 4.1 The refresh call (`oauth.go:67-74`)

```go
func (c *oauthClient) refresh(ctx context.Context, refreshToken string) (tokenPayload, error) {
    form := url.Values{
        "grant_type":     {"refresh_token"},
        "client_id":      {c.clientID},
        "refresh_token":  {refreshToken},
    }
    value, err := c.exchange(ctx, form, refreshToken)
    if errors.Is(err, provider.ErrAuthorizationDenied) {
        return tokenPayload{}, &provider.CredentialRefreshError{
            Code: "refresh_denied", Permanent: true, Cause: err,
        }
    }
    return value, err
}
```

Identical payload to the CLI. Note: `scope` is omitted (RFC-compliant — the
IdP returns the originally-granted scopes).

### 4.2 Rotation handling (`oauth.go:132`)

```go
return tokenPayload{
    AccessToken:  value.AccessToken,
    RefreshToken: firstNonEmpty(value.RefreshToken, fallbackRefresh),
    // ↑ prefers the new (rotated) token; falls back to old if IdP omits it
    ExpiresAt:    time.Now().UTC().Add(time.Duration(value.ExpiresIn) * time.Second),
    IDToken:      value.IDToken,
}, nil
```

Rotation-aware. The new refresh_token is re-encrypted and persisted via
`UpdateTokens` (`service.go:1174`).

### 4.3 Proactive refresh (`credential_scheduler.go`)

- Single `time.Timer` + DB `RefreshDueAt` index — no per-account timers.
- Lead time: `credentialRefreshAdvance = 3 * time.Minute` (CLI uses ~5 min).
- Survives restarts (schedule is persisted in the DB).

### 4.4 Reactive 401 refresh (`gateway/service.go:432-469`)

```go
if response.StatusCode == http.StatusUnauthorized {
    // SSO → can't refresh, mark reauth required
    if credential.AuthType == accountdomain.AuthTypeSSO { ... }

    // Grok Build → refresh + retry once
    refreshed, refreshErr := ensureCredential(credential, true)  // force=true
    if refreshErr == nil {
        response, err = forwardResponse(refreshed)               // retry
    }
    // still 401 after refresh → MarkReauthRequired, pull from pool
    if response.StatusCode == http.StatusUnauthorized {
        s.accounts.MarkReauthRequired(ctx, credential.ID,
            "Grok Build OAuth credential rejected after refresh")
    }
}
```

Matches the CLI's "refresh + retry once, then give up" exactly.

### 4.5 Concurrency safety (`service.go:1103-1190`)

| Mechanism | CLI | grok2api |
|-----------|-----|----------|
| In-process dedup | `Arc` flag | `s.refreshes.Do(refreshKey, ...)` singleflight |
| Cross-process lock | file lock + disk adoption | Redis lease `credential-refresh:<id>`, 2-min TTL |
| Loser adoption | re-read `auth.json` | re-read DB `latest.EncryptedAccessToken != value.EncryptedAccessToken` |

grok2api is **stronger**: the Redis lock makes it safe across multiple
grok2api instances, not just one machine.

### 4.6 Failure escalation (`service.go:1256-1301`)

```go
delays := [...]time.Duration{
    30 * time.Second, 2 * time.Minute, 5 * time.Minute,
    10 * time.Minute, 15 * time.Minute,
}
// + per-account jitter: (accountID*37)%16 seconds
// + respects server Retry-After (capped at 30 min)
// Permanent failures → MarkReauthRequired → account leaves the pool
```

More granular than the CLI's simple counter, same outcome.

---

## 5. accounts.json → grok2api round-trip

### 5.1 The farmer's output (`results/batch_*/accounts.json`)

```json
[{
  "email": "user123@example.com",
  "password": "$ExamplePass1",
  "created_at": "2026-07-10T01:46:28.802734Z",
  "tokens": {
    "access_token":  "eyJ0eXAi...",
    "refresh_token": "REDACTED...",
    "id_token":      "eyJ0eXAi...",
    "expires_at":    "2026-07-10T07:46:28.802639Z",
    "expires_in":    21600,
    "email":         "user123@example.com",
    "client_id":     "b1a00492-073a-47ea-816f-4c329264a828",
    "auth_mode":     "oidc",
    "scope":         "openid profile email offline_access ..."
  }
}]
```

### 5.2 Export conversion (`g2a_export.py` → `farm_record_to_g2a()`)

The farmer's nested `{email, tokens:{...}}` shape is flattened into
grok2api's `importedCredentialEntry`:

| farm `accounts.json` | g2a import entry | grok2api parser field |
|----------------------|------------------|-----------------------|
| `email` | `name` + `email` | `Name`, `Email` |
| `tokens.access_token` | `access_token` | `AccessToken` |
| `tokens.refresh_token` | `refresh_token` | `RefreshToken` |
| `tokens.id_token` | `id_token` | `IDToken` → JWT claims decoded |
| `tokens.client_id` | `client_id` | `OIDCClientID` (falls back to default) |
| `tokens.expires_at` | `expires_at` | `ExpiresAt` (RFC3339 parsed) |
| `tokens.expires_in` | `expires_in` | fallback if `expires_at` missing |
| `tokens.scope` | `scope` | (informational; not used by refresh) |
| — | `provider` | hardcoded `"grok_build"` |
| — | `token_type` | hardcoded `"Bearer"` |

**Fields grok2api extracts from JWT claims as fallbacks** (via
`decodeJWTClaims` in `import.go:116-119`):

- `user_id` ← JWT `sub` (e.g. `8e4f7592-6a2e-4f99-a678-2ca4f35b5b9a`)
- `email` ← JWT `email` (from `id_token`)
- `team_id` ← JWT `team_id` (e.g. `45fa6637-55b6-4ae9-b477-58f456e3a281`)
- `expires_at` ← JWT `exp` (if `expires_at` field missing)

### 5.3 Verification (real account, confirmed)

Decoding the access_token JWT payload confirms all claims are present:

```
iss            = https://auth.x.ai
sub            = 8e4f7592-6a2e-4f99-a678-2ca4f35b5b9a   → user_id
aud            = b1a00492-...                            → client_id
exp            = 1783669588                              → expires_at
scope          = openid profile email offline_access
                 grok-cli:access api:access
                 conversations:read conversations:write
principal_type = User
principal_id   = 8e4f7592-...   (= sub)
team_id        = 45fa6637-55b6-4ae9-b477-58f456e3a281
```

Running `python g2a_export.py results/accounts.json -o /tmp/test.json`
produced a valid import document that passes all `normalizeImportedCredential`
checks:

- ✅ `provider` = `"grok_build"` (accepted)
- ✅ `access_token` + `refresh_token` both present (≥1 required)
- ✅ `token_type` = `"Bearer"` (only accepted value)
- ✅ `expires_at` = RFC3339 parseable
- ✅ `client_id` matches default
- ✅ `id_token` present → `sub`/`email`/`team_id` extracted as fallbacks
- ✅ `sourceKey` = `import:hash(grok_build|client_id|identity)` for dedup

### 5.4 Dedup behavior

grok2api computes a `sourceKey` per account (`import.go:126`):

```go
identity  := firstNonEmpty(userID, email, teamID, refreshToken, accessToken)
sourceKey := "import:" + hash(provider + "|" + clientID + "|" + identity)
```

Re-importing the same account (same `user_id`/`email`) is idempotent — it
**updates** the existing record rather than creating a duplicate. This is
what makes the `g2a_pool.py` continuous importer safe to poll repeatedly.

---

## 6. Operational notes

### What the farmer should NOT do
- ❌ Refresh tokens itself (grok2api owns this)
- ❌ Rotate refresh_tokens (grok2api owns this)
- ❌ Track expiry (grok2api owns this)

### What the farmer SHOULD do
- ✅ Produce valid `{access, refresh, id_token, expires_at, client_id}`
- ✅ Export via `g2a_export.py` (handles the shape conversion)
- ✅ Let `g2a_pool.py` poll-and-import new batches (dedup is automatic)

### Token lifetime
| Stage | Duration | Owner |
|-------|----------|-------|
| Farm (PKCE login) | ~3 min per account | farmer |
| Access token validity | 6 hours (`expires_in: 21600`) | xAI |
| Refresh token validity | until revoked / rotated-out | xAI |
| Proactive refresh lead | 3 min before expiry | grok2api |
| Reactive refresh | on 401, retry once | grok2api |
| Permanent failure → reauth | account leaves pool | grok2api |

### When accounts die
An account is marked `AuthStatusReauthRequired` and pulled from the active
pool when:
1. Refresh returns a permanent error (`refresh_denied`, `missing_refresh_token`, HTTP 400/401)
2. 401 persists **after** a forced refresh + retry
3. Backoff ladder exhausts (transient failures escalate to permanent)

Dead accounts require re-farming — there is no self-healing path, because
the refresh_token is the only recovery authority and it's been rejected.
