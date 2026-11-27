# App Details

## Changes

### 1.2.0
- Add a bearer-authenticated remote Settings and control API: `GET /api/health`, `GET /api/status`, `GET/PUT /api/settings`, `POST /api/webhook/test`, and `POST /api/server/restart`, with Revision-based optimistic concurrency and human-readable validation errors.
- Add named API keys with `sms:write` and `settings:write` Scopes, immediate revocation, and phone-key rotation that leaves proxy keys intact.
- Make Webhook retry timeout, attempts, backoff, and Outbox capacity live Settings, so a longer proxy outage does not lose Inbound SMS; reject credentialed Webhook targets.
- Keep the on-device UI consistent with remote changes through fresh read-merge-write saves; `GET /api/status` reports forwarding health and warnings.

### 1.1.0
- Forward received SMS to a configurable webhook (Android), with optional HMAC-SHA256 signing, ordered delivery, retries and a persisted outbox. See the [README](./README.md#-receiving-sms-android).
- Show the last webhook delivery result and a test button in Settings; log inbound SMS and delivery outcomes.
- Request the `RECEIVE_SMS` permission.

### 1.0.1
- Adding athentication on `/control` path (not in use as of now).
- Some cleanups.

### 1.0.0
- Adding Desktop support via [EC200U Module](https://www.quectel.com/product/lte-ec200u-series/) and technically should work with other such modules via the `AT` commands.
- Both CLI & GUI can be used from Desktop platform. See the [README](./README.md) for more details.

### 0.3.0
- Service type is sticky now, should restart automatically unless stopped. [android]
- Service should run even when app is closed. [android]
- Added a stop button at notification to stop the service. [android]

### 0.2.1
- Fixing permission requirement as per Android version.
- Moving from DATA_SYNC to SPECIAL_USE service type.
- A single config source for the entire app.

### 0.2.0
- Adding a check for correct mobile number formatting.
- SMS in parts if it exits character limit.
- Showing the API endpoints and API Doc button once server is started.
- Some UI updates.

### 0.1.1
- Fixing app crash on some mobile GPU with kivymd 2.x . Thanks to [DorDeorz](https://github.com/DorDeorz) for the fix in [UniversalDownlaoder](https://github.com/DorDeorz/UniversalDownloader).

### 0.1.0
- App will check if required permissions are granted
- Adding logging for API requests
- Fixing the android bottom padding
- Adding a back button

### 0.0.1
- Initial version
