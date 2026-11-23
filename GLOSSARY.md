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
