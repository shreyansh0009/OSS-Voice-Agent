"""
Admin API: health checks, active session info, and making outbound calls.
"""
from __future__ import annotations

import os

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/admin", tags=["admin"])


class OutboundCallRequest(BaseModel):
    to: str       # phone number to call (E.164 format, e.g. "+14155551234")
    from_: str    # your Twilio number


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.post("/call/outbound")
async def make_outbound_call(req: OutboundCallRequest):
    """
    Initiate an outbound call via Twilio REST API.
    Twilio will call our /twilio/outbound webhook when the call connects.
    """
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    webhook_base = os.environ.get("WEBHOOK_BASE_URL", "https://your-ec2-host.com")

    if not account_sid or not auth_token:
        raise HTTPException(status_code=500, detail="Twilio credentials not configured")

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json"
    payload = {
        "To": req.to,
        "From": req.from_,
        "Url": f"{webhook_base}/twilio/outbound",
        "StatusCallback": f"{webhook_base}/twilio/status",
        "StatusCallbackMethod": "POST",
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, data=payload, auth=(account_sid, auth_token))

    if response.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"Twilio error: {response.text}")

    data = response.json()
    return {"call_sid": data.get("sid"), "status": data.get("status")}
