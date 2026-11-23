import hashlib
import hmac
import os
from typing import Annotated, Literal, Optional, Union

from fastapi import Body, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

app = FastAPI(title="OpenSMS Webhook Receiver")

WEBHOOK_SECRET = os.getenv("OPENSMS_WEBHOOK_SECRET", "")

class SimInfo(BaseModel):
    subscription_id: Optional[int] = None
    slot: Optional[int] = None

class WebhookTestPayload(BaseModel):
    event: Literal["webhook.test"]
    delivery_id: str
    sent_at: str

class SmsReceivedPayload(BaseModel):
    event: Literal["sms.received"]
    delivery_id: str
    received_at: str
    sent_at: str
    sender: str = Field(..., alias="from")
    text: str
    parts: int
    sim: Optional[SimInfo] = None

OpenSMSPayload = Annotated[
    Union[WebhookTestPayload, SmsReceivedPayload],
    Body(discriminator="event"),
]


def verify_signature(raw_body: bytes, signature_header: Optional[str], secret: str) -> bool:
    if not secret:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False

    expected_sig = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    received_sig = signature_header.split("sha256=", 1)[1]
    return hmac.compare_digest(expected_sig, received_sig)


@app.post("/sms-hook")
async def receive_webhook(
    request: Request,
    payload: OpenSMSPayload,
    x_opensms_event: Optional[str] = Header(None),
    x_opensms_delivery: Optional[str] = Header(None),
    x_opensms_signature: Optional[str] = Header(None),
):
    raw_body = await request.body()
    if not verify_signature(raw_body, x_opensms_signature, WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid X-OpenSMS-Signature")

    print("=" * 50)
    print(f"Event      : {x_opensms_event or payload.event}")
    print(f"Delivery ID: {x_opensms_delivery or payload.delivery_id}")
    print(f"Sent At    : {payload.sent_at}")

    if isinstance(payload, SmsReceivedPayload):
        print("-" * 50)
        print(f"From       : {payload.sender}")
        print(f"Text       : {payload.text}")
        print(f"Received At: {payload.received_at}")
        print(f"Parts      : {payload.parts}")
        if payload.sim:
            slot_str = f"Slot {payload.sim.slot}, " if payload.sim.slot is not None else ""
            print(f"SIM        : {slot_str}Sub ID: {payload.sim.subscription_id}")
    else:
        print("Status     : Test ping received successfully")

    print("=" * 50)

    return {"status": "ok", "event": payload.event, "delivery_id": payload.delivery_id}
