# Remote settings and control are served by the existing API server over a revisioned config file and a control file

The SMS proxy needs to configure and operate OpenSMS entirely over the LAN, while
the KivyMD UI and the Android foreground service remain separate processes that
both write the same persisted settings. The API server already runs inside the
service, so the endpoints belong there; the hard part is ownership, not HTTP.
See `docs/remote-api.md` for the contract.

**Status**: accepted

## Considered options

- **A second FastAPI app or process for remote control**: rejected. It would
  duplicate auth, hold a second listener, and add a lifecycle to an already
  sticky Android service with no benefit.
- **Writing settings directly from the service, bypassing `config.json`**: rejected.
  The UI reads the same file, the webhook worker hot-reloads it per delivery, and
  the port must have one owner. `config.json` stays the single source of truth;
  `request.json` carries intents only.
- **PATCH / partial-merge semantics**: rejected. Partial merges make "clear the
  secret", "leave it alone", and "the field was absent" indistinguishable against
  a UI that also holds a stale object. The API replaces the writable subset in
  full and uses an explicit empty-string sentinel to clear the secret.
- **Revision as a monotonic counter only**: rejected. A counter that no request
  checks protects nothing. `PUT` accepts an optional `revision` and refuses a
  stale one, so a client that cares gets optimistic concurrency without forcing
  it on hand-written calls.
- **Auto-restart when the port changes**: rejected. Restarts must be observable and
  debounced; `PUT` reports `restart_required`, and `POST /api/server/restart`
  performs the restart through the control file so a handler never stops its own
  uvicorn instance.
- **Keeping the single key and a single scope**: rejected. Named keys with scopes
  let the proxy hold its own credential and let the phone rotate its own without
  revoking remote access.
- **Enforcing scopes on v1 keys as stored**: rejected. v1 entries say
  `["sms:write"]` but scopes were never read, so every existing install would be
  locked out of the new API. Migration grants both scopes once; v2 keys honour
  whatever they are created with.
- **Adding an OS file lock or a new dependency**: rejected. The residual
  cross-process race window is small, documented, and cheaper to accept than to
  make buildozer ship another native dependency.
- **Unauthenticated `/api/health`**: rejected. It would reveal the device to any
  host on the LAN; the proxy already holds a key.
- **CORS or a web UI**: rejected. Server-to-server only.

## Consequences

- Two processes write `config.json`. Writes are atomic and revision-bumped, but
  there is no cross-process lock; the UI must re-read on entry and merge on save,
  and a tiny last-writer-wins window remains.
- `PUT /api/settings` is a replace of the writable subset, not a patch. Adding a
  writable field later is a breaking change for clients that do not send it.
- The webhook secret is write-only over the API; a client must GET, edit, and
  re-PUT without the secret field to preserve it.
- Port changes are two-step by design: save, then restart. A restart is a
  separate observable action, and `restart_required` tells the client when one is
  pending.
- The v1 → v2 key migration escalates every legacy key to `settings:write`. This
  is deliberate and one-time; replacing it with "honour stored scopes" would strand
  every existing install.
- Disabling the webhook still discards new Inbound SMS (the inbound spool is
  acknowledged even when forwarding is skipped). The API cannot fix that without
  an inbox-retention design, so status must warn instead of implying safety.
- Webhook retry policy becomes live configuration. The dispatcher already reloads
  `enabled`/`url`/`secret` per attempt; it now reloads `timeout`/`max_attempts`/
  `backoff` and capacity too, with today's constants as defaults.
- Tests need a config-dir seam, because both `postApi` and `appSvc` freeze the
  app-private path at import. `httpx` becomes a dev-only dependency. A CI job is
  required to run the suite; no workflow runs it today.
- The API ships untrusted-network-unsafe by design: bearer tokens over HTTP, no
  TLS, no CORS. Deployments must stay on the LAN or a VPN/tunnel.
