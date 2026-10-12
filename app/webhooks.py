from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from . import paypal_client as pp

router = APIRouter()
EVENTS: list[dict] = []


@router.post("/webhooks/paypal")
async def paypal_webhook(request: Request):
    event = await request.json()
    if not pp.verify_webhook(request.headers, event):
        raise HTTPException(400, "Invalid webhook signature")

    res = event.get("resource", {})
    inv = res.get("invoice", res)
    number = inv.get("detail", {}).get("invoice_number") or inv.get("id", "")
    etype = event.get("event_type", "")
    labels = {
        "INVOICING.INVOICE.PAID": f"Invoice {number} was paid",
        "INVOICING.INVOICE.CANCELLED": f"Invoice {number} was cancelled",
        "INVOICING.INVOICE.REFUNDED": f"Invoice {number} was refunded",
    }
    if etype in labels:
        EVENTS.append(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "type": etype,
                "summary": labels[etype],
            }
        )
        del EVENTS[:-50]
    return {"ok": True}