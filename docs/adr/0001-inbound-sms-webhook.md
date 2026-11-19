# Inbound SMS is captured by the service and delivered by an ordered webhook outbox

OpenSMS was outbound-only: a sticky foreground service on Android runs the FastAPI sender and survives the UI being closed. To forward Inbound SMS, that service registers an `SMS_RECEIVED` receiver at runtime; the receiver appends each message to a private inbox file, and the service drains the file and delivers Webhooks from a single ordered worker backed by a persisted Outbox with bounded retries. The app deliberately does not become the default SMS app: that role would require handling `SMS_DELIVER` and the whole messaging surface. The receiver never does network I/O, and delivery stays outside the API server so Webhooks work whether or not the HTTP API is running.

**Status**: accepted

## Considered options

- **Default SMS app (`ROLE_SMS`)**: guarantees routing, but requires handling `SMS_DELIVER` (and deduplicating against `SMS_RECEIVED`), writing to the SMS provider, compose intents and notifications.
- **Manifest-declared receiver**: buildozer ignores `android.extra_manifest_application_xml` (even on master), so the declaration never reaches the merged manifest. Patching the build tool was rejected as fragile.
- **Python `BroadcastReceiver` subclass through pyjnius**: works, but subclassing Android classes from Python is brittle; a thin Java receiver is easier to reason about.
- **Coupling Webhook delivery to the API server**: forces users to keep the API running to receive events.
- **Fire-and-forget delivery**: silently loses messages on flaky LANs.

## Consequences

- Capture exists only while the service process is alive. If the OS kills the service and no new SMS arrives, nothing is forwarded; reboot auto-start is deliberately out of scope and documented.
- The receiver is registered only after `RECEIVE_SMS` is granted; the service retries registration every 10 seconds.
- The webhook payload is a public contract. Multipart SMS is assembled into one Inbound SMS, and the sender is the raw originating address (it may be alphanumeric, not E.164).
- Delivery is at-least-once with bounded retry, so a receiver can see duplicates after a timeout or a lost response.
- The Outbox is capped at 100 pending Webhooks; the oldest is dropped (and logged) when full.
- LAN targets require the phone and the receiver to share a network; the README states this.
- The Settings "Send test" action runs in the UI process and posts directly, so it does not depend on the service being alive.
