# Remote Settings & Control API

**Status:** implemented; ships as `1.2.0`.
**Supersedes:** the handoff brief of 2026-11-26.
**Baseline:** `develop` at `f3c7ba1`, app version `1.1.0`.
**Target:** ship as `1.2.0`; PRs go to `develop` (see `CONTRIBUTE.md`).

This document is the implementation description. It replaces the pasted handoff
brief, corrects three claims in it that do not match the code, and settles the
questions the brief left open.

---

## 1. Mission

An SMS proxy server on the LAN owns blocking and forwarding rules and needs to
operate the phone without anyone touching it. Today OpenSMS exposes
`POST /sms/` and forwards inbound SMS to a webhook, but every setting lives
behind the KivyMD Settings screen.

Deliver a bearer-authenticated JSON API, served by the FastAPI app that already
runs inside the Android foreground service, that lets the proxy:

- read and write the writable settings (port, launch behaviour, webhook target,
  secret, retry policy);
- read health and status;
- test the webhook;
- restart the API server after a port change;
- manage named API keys with scopes.

No web UI. No new runtime dependencies. No unauthenticated routes.

---

## 2. What the code actually does

Verified against this checkout. The brief's line references are accurate to
±2. Three of its claims are wrong and shape the design:

1. **The inbound spool is not deleted unconditionally.** In
   `app/services/appSvc.py:196-218`, a claimed message is acknowledged when
   `enqueue()` returns `True` **or `False`** (webhook disabled or target
   invalid), and deliberately kept as `.processing` when `enqueue()` raises.
   Data loss therefore happens when forwarding is **disabled**, not when it
   fails. The at-least-once guarantee from
   `docs/adr/0001-inbound-sms-webhook.md` must be preserved.
2. **`keys.json` v1 entries already carry `scopes`** (`["sms:write"]`), but
   nothing ever reads them (`app/services/postApi.py:220-243`). Adding
   enforcement is a new capability. "Migrate to both scopes" is therefore a
   one-time privilege escalation, not a repair.
3. **`config_dir` is computed at import time** in `postApi.py:50` and
   `appSvc.py:76`. A test seam must redirect the module globals; adding a setter
   that nothing consumes is not enough.

Other facts that constrain the work:

| Area | Reality |
|---|---|
| Processes | Kivy UI and the `opensmssvc` foreground service are separate. The API server runs in the service. |
| `config.json` | Whole-object writes from the UI (`main.py:353-366`), atomic, no revision, no lock. Webhook config is re-read per delivery attempt (`webhookSvc.py:50-57`). |
| `request.json` | UI→service command channel polled every 0.2 s (`appSvc.py:233-260`); written atomically through `controlSvc`. |
| Restart | No restart path exists. `api_server_control` handles only `start`/`stop` (`appSvc.py:156-170`), and `start` uses `req_data["port"]`. |
| Keys | One key. `generate_new_api_key()` overwrites `keys.json` (`postApi.py:92-109`). Raw format `osms_<token_urlsafe(32)>`. |
| Outbox | Capacity drop-oldest at enqueue; `max_attempts` then drop (`webhookSvc.py:318-339`, `386-415`). Retry knobs are constructor-fixed; only `enabled`/`url`/`secret` hot-reload per attempt. |
| Version | Single source `app/services/config.py:3`; buildozer and CI derive from it. |
| Tests | One `unittest` file (`test_webhook.py`, local `ThreadingHTTPServer`). `httpx` is absent everywhere. No workflow runs tests; CI is build-and-release on `main` only. |
| Desktop imports | `postApi` imports `serial`/`psutil` but opens no port; no Kivy import. Importing it in desktop tests is safe. |
| Port rule | UI label says 1025–65535, code only checks `>= 1025` (`main.py:636-640`). |

---

## 3. Scope

### In scope

1. JSON endpoints for all persistent settings that make sense remotely.
2. Operational endpoints: health, status, webhook test, server restart.
3. Named API keys with scopes; v1 read compatibility; no hash or secret leakage.
4. Cross-process config safety: atomic writes, revision counter, UI merge-on-save.
5. Live-configurable webhook retry policy and outbox capacity.
6. Tests (`unittest`), CI job, README/docs, one ADR, glossary terms.

### Out of scope

- Any HTML/CSS/JS UI, cookies, sessions, CORS, TLS.
- Blocking/forwarding rules, message history, delivery receipts, SIM selection.
- Persisting inbound SMS beyond the existing spool/outbox.
- Remote Android permission grants or app relaunch.
- Desktop serial settings in the remote API.
- Changing the key hashing algorithm (stay SHA-256).
- `/api/logs` — specified in Appendix A, deferred to a later change.

---

## 4. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | Reuse the existing module-level FastAPI app in `postApi.py`. No second server, no static assets, no `buildozer.spec` changes. | The service already runs it; a second listener adds a lifecycle problem with no benefit. |
| D2 | Keep `HTTPBearer`. Add `require_scope(...)`: `/sms/` needs `sms:write`, every `/api/*` route needs `settings:write`. | Scopes already exist in the data model; enforcing them is cheap and makes limited keys possible. |
| D3 | `PUT /api/settings` replaces the **writable subset** in full. Unknown fields are rejected. Internal fields (`app`, `permissions`) and unknown disk fields are preserved. | Full replace avoids ambiguous patch semantics; preserving internal fields keeps the UI's config intact. |
| D4 | Every write bumps `revision`. `PUT` accepts an **optional** top-level `revision`; if present and stale, return `409`. | Two remote writers otherwise last-writer-wins. Optional keeps hand-written clients simple. |
| D5 | Restart is explicit and goes through `request.json`. A handler never calls `stop()` on its own server. `409` if already stopping/stopped, or if a restart happened within 5 s. | Stopping uvicorn from inside a request risks answering into a dead socket. |
| D6 | `config.json` is the single source of truth for the port. `request.json` carries intents; the Restart intent includes the port to start with, used only as a fallback. | The UI currently seeds the port twice; one owner removes a class of drift. |
| D7 | All JSON writes are atomic: temp file in the same directory, `os.replace`. | A 0.2 s poller can otherwise read a partial file. |
| D8 | The UI re-reads `config.json` on Settings entry and merges on save through `settingsSvc`, preserving `revision` and unknown fields. | Today the UI holds a stale whole-object cache and clobbers remote changes. |
| D9 | `keys.json` v2: array of `{id, name, hash, scopes, created_at, owner}`. Refuse to delete the last key (`409`). UI rotation replaces only the phone-owned key. | Named keys need stable ids; nuking proxy-owned keys from the phone would be hostile. |
| D10 | Status is best-effort: runtime providers first, files second, honest `null`/`"unknown"` when neither exists. Add `warnings[]`. | `resp.json` records transitions, not liveness. |
| D11 | Retry policy (`timeout`, `max_attempts`, `backoff`, `capacity`) is a setting, read live per attempt/enqueue. Defaults stay exactly today's constants. | This is the data-loss mitigation; it belongs in the first release, not a follow-up. |
| D12 | Tests use `unittest` + `TestClient` against a temporary config dir. `httpx` becomes a dev-only dependency. Add a test workflow. | No Android needed; today nothing runs the suite. |
| D13 | Bump `__version__` to `1.2.0`, add a CHANGELOG entry, one ADR (`0002`), glossary terms, README section. | The tag is derived from `config.py`; docs are the repo's existing habit. |
| D14 | Baseline is `develop` at `f3c7ba1`. `0756bdc` is the `v1.1.0` merge into `main` and is **not** an ancestor of this line. | The brief's "verify against 0756bdc" is unverifiable here. |

---

## 5. Vocabulary

See `GLOSSARY.md`: **Settings**, **Revision**, **Scope**, **API key**,
**Restart**. **Outbox** remains webhook-only.

---

## 6. API contract

### 6.1 Conventions

- Base: `http://<phone-lan-ip>:<port>`.
- Every route, including `/api/health`, requires
  `Authorization: Bearer osms_…`.
- JSON in, JSON out, UTF-8.
- Strict Pydantic request models expose editable JSON bodies for
  `PUT /api/settings` and `POST /api/keys`. They reject unknown fields and
  lax JSON coercion (including strings/floats/booleans where integers are
  required). Pydantic validates only the HTTP wire shape.
- Domain and persistence stay in the owning services:
  `settingsSvc.save_settings` owns revision checks, business rules, merge-
  preserving unknown stored keys, and the atomic write; `keySvc` owns key
  validation and storage.
- Validation order is wire shape/range → `422`, stale revision → `409`, then
  Settings business rules → `400`.
- Error body: `{"detail": "..."}`. A shared `RequestValidationError` handler
  keeps `422` details human-readable for schema errors and malformed JSON;
  clients should not parse Pydantic's internal error-list shape.
- Status codes:
  - `400` business-rule violation
  - `401` invalid key
  - `403` missing `Authorization` header (HTTPBearer default) or missing scope
  - `404` unknown resource
  - `409` conflict (stale revision, last key, restart debounce/stopped)
  - `422` schema/range/unknown-field violation
  - `502` webhook test delivery failed
- Never return: webhook secret, key hashes, raw keys after creation, a full
  Webhook target with credentials. A legacy target's userinfo is removed from
  Settings responses without changing the stored value.

### 6.2 `GET /api/health`

```json
{"status": "ok", "version": "1.2.0", "platform": "android", "uptime_s": 812}
```

`uptime_s` is seconds since the current `PosiApiServer` instance started; `0`
when unknown. `platform` comes from `get_os_platform()`.

### 6.3 `GET /api/status`

```json
{
  "version": "1.2.0",
  "api": {"running": true, "port": 9292, "bound_port": 9292, "uptime_s": 812},
  "service": {"server": "started", "serial": "none", "provider": "none"},
  "config": {"port": 9292, "boot": true, "revision": 7},
  "network": {
    "wifi": [{"interface": "wlan0", "ip": "192.168.1.50"}],
    "ethernet": []
  },
  "webhook": {
    "enabled": true,
    "url_host": "proxy.example.lan",
    "secret_set": true,
    "outbox_pending": 0,
    "outbox_capacity": 100,
    "last_delivery": {
      "ok": true, "event": "sms.received", "delivery_id": "...",
      "http_status": 204, "attempts": 1, "error": null,
      "at": "2026-11-26T12:34:56+01:00"
    }
  },
  "warnings": []
}
```

Rules:

- `api.running`: provider value; default `true` when no provider is registered
  (the response itself proves reachability).
- `api.bound_port`: provider value, `null` if unknown.
- `config`: from `settingsSvc.load_settings`; `null` only if the file is
  unreadable in a way loading cannot repair.
- `service`: `resp.json` values, `"unknown"` individually if absent/unreadable.
- `network`: `get_network_ips()`; any failure yields empty lists and adds
  `{"code": "network_unavailable"}`.
- `webhook.url_host`: `urlparse(url).hostname`, `null` if empty. Never path,
  query, or credentials.
- `last_delivery`: `null` when no status file. Credentialed target URLs and
  details of the current target are scrubbed from its error text.
- `outbox_pending`: provider value, `null` when unavailable.
- `warnings` is always an array. Codes:
  - `webhook_disabled` — accepted but every new Inbound SMS is discarded, with
    a message saying exactly that.
  - `webhook_url_invalid` — enabled with a target that fails
    `is_valid_target`.
  - `outbox_near_capacity` — `pending >= 0.8 * capacity`.

### 6.4 `GET /api/settings`

```json
{
  "revision": 7,
  "settings": {
    "port": 9292,
    "boot": true,
    "webhook": {
      "enabled": true,
      "url": "https://proxy.example.lan/hooks/opensms",
      "secret_set": true,
      "timeout": 10,
      "max_attempts": 3,
      "backoff": [5, 30],
      "capacity": 100
    }
  }
}
```

Never include `permissions`, `app`, or the raw secret. If a stored legacy
target contains URL userinfo, `webhook.url` is returned with that userinfo
removed; the persisted value remains unchanged and invalid for forwarding.

### 6.5 `PUT /api/settings`

```json
{
  "revision": 7,
  "port": 9292,
  "boot": true,
  "webhook": {
    "enabled": true,
    "url": "https://proxy.example.lan/hooks/opensms",
    "secret": "new-shared-secret",
    "timeout": 10,
    "max_attempts": 3,
    "backoff": [5, 30],
    "capacity": 100
  }
}
```

Required: `port`, `boot`, `webhook` and all webhook fields except `secret`.
Optional: `revision`, `webhook.secret`.

Secret sentinel:

- omitted → keep the stored secret
- `""` → clear it
- non-empty string → replace it (max 256 characters)

Validation:

| Field | Rule | Failure |
|---|---|---|
| `port` | integer, `1025 <= port <= 65535` | `422` |
| `boot` | JSON boolean | `422` |
| `webhook.enabled` | JSON boolean | `422` |
| `webhook.url` | string; required; credential-free valid `http(s)://host[:port]/path` when `enabled` is true; may be `""` when disabled; URL userinfo is always rejected | `422` if wrong type; `400` if enabled and invalid/empty or if userinfo is present |
| `webhook.secret` | string, ≤ 256 chars, sentinel semantics | `422` |
| `webhook.timeout` | integer 1–60 (seconds) | `422` |
| `webhook.max_attempts` | integer 1–50 | `422` |
| `webhook.backoff` | list of 1–8 positive integers, each ≤ 3600 | `422` |
| `webhook.capacity` | integer 10–5000 | `422` |
| unknown key, any level | rejected | `422` |
| `revision` | integer; if present and ≠ current → conflict | `409` |

Order of checks: schema/range (`422`) → revision (`409`) → business rule
(`400`) → write.

Write procedure:

1. `load_settings` from disk (fresh read).
2. Apply the writable fields; preserve `app`, `permissions`, and unknown keys.
3. Re-validate the merged object.
4. `revision = current.revision + 1`; atomic write.
5. Log field names only: `Settings updated: port=9292 webhook.enabled=true secret=set`.

`200`:

```json
{
  "revision": 8,
  "restart_required": false,
  "settings": { "...": "same shape as GET" }
}
```

`restart_required` is `true` when the saved `port` differs from the live
`bound_port` (provider) while the server is running. It is a statement about
current state, not about this request: a previously pending port change also
reports `true`.

Changing `boot` is read at the next app launch; it does not affect the current
service. Changing the Webhook or retry fields never requires a restart.

### 6.6 `POST /api/webhook/test`

Optional body:

```json
{"url": "https://proxy.example.lan/hooks/opensms", "secret": "..."}
```

- No body → saved webhook target and secret.
- `url` present → used for this test only, must be valid and credential-free
  (`400` otherwise); it is allowed while `enabled` is false.
- `secret` present → used for this test only; `""` means unsigned.
- Uses the saved `timeout`.

`200`: `{"ok": true, "http_status": 204, "error": null}`
`502`: `{"ok": false, "http_status": 500, "error": "HTTP 500"}`

3xx counts as failure. Uses `webhookSvc.send_test_webhook`.

### 6.7 `POST /api/server/restart`

`202`:

```json
{"status": "restarting", "port": 9292}
```

Rules:

- `409` if the API is not running (provider) or a restart was requested within
  the last 5 seconds.
- The handler reads the effective port from `config.json`, schedules the
  control-file write **after** the response (`RESTART_DELAY = 0.4 s`), and
  returns.
- The intent is written atomically as
  `{"port": <effective port>, "server": "restart", "restart_id": "<token>"}`
  merged into the existing `request.json`, preserving `serial_port`,
  `baudrate`, and unknown keys. The service uses `restart_id` to suppress only
  the stale intent that could not be normalised; a later Restart gets a new id
  and is handled normally.
- The handler never calls `PosiApiServer.stop()`.

Service side (`appSvc`):

1. Handle `"restart"`: `stop()`, sleep 0.5 s, start with
   `load_settings(config.json)["port"]`, falling back to `request.json["port"]`.
2. Write `resp.json` with `"started"`.
3. Rewrite `request.json` with `"server": "start"` and the effective port,
   preserving other keys.
4. The 0.2 s loop then sees `"start"` with the server already started and does
   nothing. No restart loop.

### 6.8 Keys

`POST /api/keys`

```json
{"name": "sms-proxy", "scopes": ["sms:write", "settings:write"]}
```

`scopes` is optional; default is both scopes. It must be a non-empty subset of
`{"sms:write", "settings:write"}`.

`201`:

```json
{
  "id": "k_9f2a1c4d",
  "name": "sms-proxy",
  "key": "osms_…",
  "scopes": ["settings:write", "sms:write"],
  "created_at": "2026-11-26T12:00:00+00:00"
}
```

The raw `key` appears only here.

`GET /api/keys`

```json
{"keys": [
  {"id": "k_9f2a1c4d", "name": "sms-proxy",
   "scopes": ["settings:write", "sms:write"],
   "created_at": "2026-11-26T12:00:00+00:00"}
]}
```

Never return hashes.

`DELETE /api/keys/{id}` → `204`; `404` unknown/malformed id; `409` if it is the
last remaining key. Deleting the key used for the current request is allowed
when another remains and takes effect immediately.

Rules:

- Name: trimmed, 2–64 characters, no control characters, unique
  case-insensitively; `opensms` is reserved case-insensitively for the phone
  key and is rejected on proxy-key creation (`422`); duplicate → `409`.
- Id: `k_` + 8 lowercase hex (`secrets.token_hex(4)`).
- Scopes stored canonical-sorted.
- Ownership stored internally with an `owner` marker (`phone` or `proxy`);
  rotation uses that marker, never the display name. Ownership is normalized
  for legacy stores. The marker and hashes are never returned by `GET`.
- Hash: SHA-256 hex, untruncated (unchanged).
- Lookup: scan every entry with `secrets.compare_digest` against the computed
  hash; do not return early on the first mismatch.
- Writes are atomic.

### 6.9 `POST /sms/` (unchanged)

Requires `sms:write`. Boolean success means "handed to Android/SIM", not
delivered. Numbers must be E.164. Default SIM only.

---

## 7. Persisted schemas

`config.json` (v1 gains `revision` and webhook retry fields):

```json
{
  "app": "opensms",
  "port": 9292,
  "boot": false,
  "permissions": false,
  "revision": 7,
  "webhook": {
    "enabled": false,
    "url": "",
    "secret": "",
    "timeout": 10,
    "max_attempts": 3,
    "backoff": [5, 30],
    "capacity": 100
  }
}
```

- Missing `revision` → `0` on load; first save writes `1`.
- Missing retry fields → current constants. Unknown fields are preserved.
- The secret stays plaintext at rest, as today. Masking is API-level only.

`keys.json` v2:

```json
{
  "version": 2,
  "keys": [
    {
      "id": "k_9f2a1c4d",
      "name": "sms-proxy",
      "hash": "<sha256 hex>",
      "scopes": ["settings:write", "sms:write"],
      "created_at": "2026-11-26T12:00:00+00:00",
      "owner": "proxy"
    }
  ]
}
```

v1 migration (`{hash: {name, scopes}}`):

- Converted in memory on first load; persisted as v2 on the first key write or
  on the first authenticated request (whichever comes first), guarded by a
  module lock and the shared `keys.json.lock` OS file lock.
- Ids are newly generated; `created_at` becomes "now" (original unknown);
  missing names become `"opensms"`.
- Scopes: existing scopes **plus** `settings:write`. This is a one-time
  escalation; it is deliberate because v1 scopes were never enforced and every
  existing install would otherwise lock itself out of the new API. Documented
  in the ADR.

---

## 8. Cross-process behaviour

Two processes write `config.json`, and the UI and API may mutate `keys.json`.
Accepted model:

- **Writers**: the API (one uvicorn thread pool) and the Kivy UI.
- **In-process serialisation**: a module lock around read-modify-write in
  `settingsSvc`; `keys.json` mutations use one module lock.
- **Key-store cross-process serialisation**: `keys.json` read-modify-write
  operations use a stdlib OS file lock at `keys.json.lock`, with no new
  dependency. This covers key creation, deletion, phone-key rotation,
  authentication migration, and direct atomic key-store saves.
- **Cross-process**: last writer wins, but the window is shrunk to
  read-fresh-then-write, and every writer bumps `revision` against the value it
  actually read. The residual race is documented, not hidden.

UI merge-on-save:

1. On Settings entry: `reload_settings()` re-reads `config.json`, updates
   `config_template`, `req_template["port"]`, the port field, the boot switch,
   the webhook fields, and the last-delivery label. (Replaces
   `reload_webhook_config`.)
2. On save: build a patch from the form (`port` if non-empty, `boot`, webhook
   `enabled`/`url`/`secret`), call
   `settingsSvc.save_settings(config_path, patch)`.
3. Invalid port (< 1025 or > 65535) or enabled-without-valid-URL rejects the
   **whole** save with a toast. No partial write.
4. Adopt the returned object into `config_template` and refresh the views.

`write_request()` becomes atomic and writes `port` from the last loaded
settings.

---

## 9. File-by-file change plan

| File | Change |
|---|---|
| `app/services/settingsSvc.py` | **New.** `DEFAULT_SETTINGS`, `load_settings`, `save_settings(..., expected_revision=None)`, `validate_settings`, `apply_writable_settings`, `writable_settings`, `atomic_write_json`. No Kivy/jnius/Android imports. Reuses `webhookSvc.is_valid_target`. |
| `app/services/postApi.py` | Routes from §6; `_runtime` providers + `set_runtime_providers` + `set_config_dir`; `require_scope`; keys v2 load/migrate/create/list/delete; `PosiApiServer.bound_port`/`started_at`; restart debounce; remove the `/control/` placeholder (its shape is superseded). |
| `app/services/appSvc.py` | Register providers after constructing server/dispatcher; call `set_config_dir`; `"restart"` handling; start from `config.json` port; normalise the control file; keep spool ack semantics unchanged. |
| `app/services/webhookSvc.py` | `normalize_webhook_config` carries retry knobs with defaults; `enqueue` uses live capacity; `_deliver` reads live timeout/max-attempts/backoff. Constructor args default to `None` (live config → constant) and stay explicit overrides so existing tests keep working. |
| `app/main.py` | Replace cache-only writes with `settingsSvc` read-merge-write; `reload_settings` on entry and after save; atomic `write_request`; key rotation replaces only `"opensms"`; update the API-key supporting text. |
| `app/screens/settings.py` | Supporting text only: rotation wording; port label already says 1025–65535 and now matches validation. No new fields. |
| `app/tests/test_settings_svc.py` | **New.** Pure module tests (defaults, revision, atomicity, secret sentinel, unknown fields, merge preserving internal fields). |
| `app/tests/test_remote_api.py` | **New.** Endpoint/auth/keys/restart/status tests via `TestClient`. |
| `app/requirements-dev.txt` | **New.** `httpx`. Dev only; Android and desktop runtime requirements unchanged. |
| `.github/workflows/tests.yml` | **New.** Ubuntu, Python 3.13, install `fastapi uvicorn httpx psutil pyserial`, run `python -m unittest discover -s tests -p "test_*.py" -v` from `app/`, path-filtered to `app/**`. |
| `app/services/config.py` | `__version__ = "1.2.0"`. |
| `CHANGELOG.md` | `### 1.2.0` entry (heading must match `__version__` exactly). |
| `README.md` | "Remote API" section with curl examples, scopes, LAN/VPN warning; feature bullet. |
| `docs/adr/0002-remote-settings-and-control-api.md` | **New.** This design's ADR. |
| `GLOSSARY.md` | New terms. |
| `app/buildozer.spec` | No change. Verify no static assets or dev requirements leak in (`tests` is already excluded). |

---

## 10. Tests

Framework: `unittest`, consistent with `app/tests/test_webhook.py`.
`TestClient` from `fastapi.testclient` (requires dev `httpx`).

Isolation:

- `tempfile.TemporaryDirectory()` per test.
- `postApi.set_config_dir(tmp)`; reset `_runtime`, `_last_restart_at`,
  `postApi._started_at`.
- `postApi.set_api_logger(dummy)` where the dummy swallows writes.
- Never import `appSvc` in tests: it creates real directories and imports
  service-only code at module scope.

Required cases:

1. `GET /api/settings` on a missing `config.json` returns defaults; no secret.
2. `PUT` valid payload: revision increments, file is valid JSON and atomic
   (no `*.tmp` left), response masks the secret, `restart_required` false when
   the port is unchanged.
3. `PUT` validation: port < 1025 / > 65535 → `422`; `enabled: true` with
   missing/invalid URL → `400`; unknown top-level and nested field → `422`;
   secret omitted keeps the old value; `""` clears it.
4. `PUT` stale `revision` → `409`; matching `revision` → `200`.
5. `PUT` port change with a fake `bound_port` provider → `restart_required:
   true`; port equal → `false`.
6. Auth: valid key → `200`; bad key → `401`; missing header → HTTPBearer's
   `403`; key without `settings:write` → `403`; `/sms/` still requires
   `sms:write`.
7. `GET /api/status`: merges providers and `resp.json`; unreadable files →
   `"unknown"`/`null`; network failure → empty lists + warning; URL masked to
   host; `webhook_disabled` warning present when disabled.
8. `POST /api/server/restart`: `202`; `request.json` gets `server=restart`
   while preserving `serial_port`/`baudrate`; second call within 5 s → `409`;
   stopped provider → `409`; debounce resets.
9. `POST /api/webhook/test`: success against a local `ThreadingHTTPServer`
   (reuse the `test_webhook.py` `MockTarget` pattern); failure → `502`;
   URL override works without saving.
10. Keys: create returns a raw key once and it authenticates; `GET` has no
    hashes; duplicate name (case-insensitive) → `409`; unknown delete → `404`;
    last key → `409`; deleting the calling key with another present → `204` and
    the old key stops working; v1 file migrates, authenticates, and is
    persisted as v2.
11. UI merge: after an API write, a simulated UI save built from a stale
    `config_template` preserves the API-changed Webhook target and the incrementing
    revision.
12. Live retry config: dispatcher picks up `max_attempts`/`backoff`/`capacity`
    from `config.json` when constructor args are absent; constructor args still
    override (existing tests must pass unchanged).

Run:

```bash
cd app
python -m unittest discover -s tests -p "test_*.py" -v
```

Manual Android smoke (not CI): build, start the service, then from a laptop on
the same Wi-Fi: `GET /api/health`, `GET /api/settings`, `PUT /api/settings`,
`POST /api/server/restart`; confirm the proxy receives `sms.received` and that
`request.json` ends as `"start"`.

---

## 11. Phases

| Phase | Content | LOC (src) |
|---|---|---|
| 1 | `settingsSvc.py`, schema, atomic writes, revision, validation, secret sentinel | 150–200 |
| 2 | Provider hooks, config-dir seam, scopes, `GET/PUT /api/settings` | 100–140 |
| 3 | `/api/health`, `/api/status`, `/api/webhook/test` | 80–120 |
| 4 | Restart orchestration + UI merge-on-save + atomic `request.json` | 100–160 |
| 5 | Keys v2, migration, CRUD | 110–160 |
| 6 | Live webhook retry/capacity (dispatcher + schema already in Phase 1) | 60–100 |
| — | Tests, docs, ADR, CI | 250–350 |

Core total ≈ 600–900 source lines, 3–4 focused days.

---

## 12. Acceptance criteria

1. With only `Authorization: Bearer osms_…`, the proxy can read and write every
   writable setting, test the webhook, restart the server, and manage keys.
2. `GET /api/settings` never reveals the secret; `GET /api/keys` never reveals
   hashes; raw keys appear only in the creation response.
3. Webhook target/secret/enable and retry changes take effect on the next inbound
   SMS without a restart.
4. A port change followed by `POST /api/server/restart` leaves the API listening
   on the new port; `request.json` ends at `"start"`; no restart loop; a bind
   failure is visible as `bound_port: null` and a log entry.
5. An API settings change survives a subsequent UI save (merge test).
6. Invalid payloads return `4xx` with a descriptive `detail` and never leave a
   partial or corrupt `config.json`.
7. `unittest` passes on desktop without Android imports; the new CI job runs it.
8. `POST /sms/` and the phone UI behave as before for valid inputs.
9. `GET /api/status` warns when forwarding is disabled, naming the data loss.
10. README documents the API, scopes, curl examples, and LAN/VPN-only guidance.

---

## 13. Risks

1. **Restart loop** if `request.json` stays `"restart"`. Normalise to `"start"`
   immediately after starting.
2. **Self-deadlock** if a handler stops its own server. Always go through the
   control file.
3. **Response/stop race**: the 202 must reach the client before uvicorn shuts
   down. The `RESTART_DELAY` timer is the mitigation; do not lower it below
   ~0.3 s.
4. **UI clobbering** remains possible in the tiny window between the UI's fresh
   read and its replace. Accepted; documented.
5. **Ack-on-disabled discards SMS.** A remote client that disables the webhook
   destroys every SMS received while it is off. The status warning must say so.
6. **Outbox full drops the oldest** record. Capacity is now configurable, but
   the drop policy is unchanged.
7. **`resp.json` is not a heartbeat.** Do not present it as liveness; the
   response itself proves the API is up.
8. **Pydantic request models are an HTTP edge, not a second domain layer.**
   Strict types and `extra="forbid"` validate the wire shape; `settingsSvc` and
   `keySvc` retain revision, business-rule, merge, and persistence ownership.
9. **Bind errors are swallowed** into a callback (`postApi.py:173-178`). The new
   status fields make the failure visible; they do not fix it.
10. **Key deletion** can lock out the caller only if it is the last key; `409`
    prevents that. Rotation from the phone only touches the `opensms` key.
11. **No TLS/CORS.** LAN or VPN only. `/docs` is public but requires a key to
    execute.

---

## Appendix A — deferred `/api/logs`

`GET /api/logs?lines=200` (default 200, max 1000) → `{"lines": [...]}`.

- Source: `LogService.get_api_logs()`, last N lines, trailing newlines stripped.
- Never expose file paths.
- Missing file → `{"lines": []}`.
- Requires `settings:write` like every other `/api/*` route.

Not in the `1.2.0` change.

## Appendix B — deferred, explicitly

Inbound SMS persistence/history, delivery receipts, SIM selection, remote
permission grants, serial settings over the API, TLS termination, CORS, a web
UI.
