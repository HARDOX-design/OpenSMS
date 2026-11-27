# OpenSMS

OpenSMS turns a phone or a USB modem into a local SMS gateway: it sends SMS on request and notifies a configured endpoint when SMS arrives.

## Language

**Inbound SMS**:
An SMS received by the device's SIM, as opposed to one sent through the OpenSMS API.
_Avoid_: incoming message, received text, downlink

**Webhook**:
A notification that OpenSMS delivers over HTTP to announce an event, such as an Inbound SMS.
_Avoid_: callback, post, push

**Webhook target**:
The URL that Webhooks are delivered to.
_Avoid_: endpoint, destination, webhook URL

**Delivery attempt**:
One HTTP request made while trying to deliver a Webhook.
_Avoid_: retry, send, try

**Outbox**:
The persisted queue of Webhooks that are waiting to be delivered.
_Avoid_: queue, buffer, backlog

**Settings**:
The operator-chosen configuration that OpenSMS keeps across restarts: the API port, whether the server starts at launch, and the Webhook configuration.
_Avoid_: config, preferences, options

**Revision**:
The identifier of one persisted version of Settings. A write that expects an older Revision than the stored one is refused.
_Avoid_: version, generation, counter

**Scope**:
A permission attached to an API key that allows a class of operations, such as sending SMS or changing Settings.
_Avoid_: role, permission level, grant

**API key**:
The secret credential an external client presents to call the OpenSMS API.
_Avoid_: token, password, secret

**Restart**:
Replacing the running API server with a new one that uses the current Settings, notably the port.
_Avoid_: reload, reboot, bounce
