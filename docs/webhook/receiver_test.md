# Webhook receiver (simple)
A simple receiver to test webhook manually. It acts as a receiving server.

## Code path
Here is the [code](../../app/tests/webhook_receiver.py)

## Install dependencies
```bash
pip install fastapi uvicorn
```

## Run the receiver
```bash
cd to-your-code-path/
uvicorn webhook_receiver:app --host 0.0.0.0 --port 8000 --reload
```

## Test with sample data
```bash
curl -X POST http://localhost:8000/sms-hook \
  -H "Content-Type: application/json" \
  -H "User-Agent: OpenSMS/1.1.0" \
  -H "X-OpenSMS-Event: sms.received" \
  -H "X-OpenSMS-Delivery: 6f1c2f5e-0d7a-4c55-9f0e-2b1c8b7f0a11" \
  -H "X-OpenSMS-Signature: sha256=dummy" \
  -d '{
    "event": "sms.received",
    "delivery_id": "6f1c2f5e-0d7a-4c55-9f0e-2b1c8b7f0a11",
    "received_at": "2026-11-19T14:32:10+05:30",
    "sent_at": "2026-11-19T09:01:58+00:00",
    "from": "+14155552671",
    "text": "Hello from a real SMS",
    "parts": 1,
    "sim": {"subscription_id": 1, "slot": 0}
  }'
```
