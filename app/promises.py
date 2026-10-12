import json
import os
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import paypal_client as pp
from .webhooks import EVENTS

router = APIRouter(prefix="/api/promises")

# invoice_id -> promise. status: active, kept, broken, paid_late, closed
PROMISES: dict[str, dict] = {}
ALLOW_PAST = os.getenv("PROMISE_DEMO_MODE") == "1"  # lets you demo a broken promise without waiting
_PAID = ("PAID", "MARKED_AS_PAID")
_CLOSED = ("CANCELLED", "REFUNDED")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event(text: str) -> None:
    EVENTS.append({"time": _now(), "type": "PROMISE", "summary": text})
    del EVENTS[:-50]


def get_active(invoice_id: str) -> dict | None:
    p = PROMISES.get(invoice_id)
    return p if p and p["status"] == "active" else None


def save(inv: dict, promised_date: str, note: str = "") -> dict:
    d = date.fromisoformat(promised_date)
    if d < date.today() and not ALLOW_PAST:
        raise ValueError("The promised date is in the past.")
    if d > date.today() + timedelta(days=120):
        raise ValueError("The promised date is too far away.")
    PROMISES[inv["id"]] = {
        "invoice_id": inv["id"],
        "number": inv["number"],
        "client": inv["client"],
        "promised_date": d.isoformat(),
        "note": note[:300],
        "status": "active",
        "created": _now(),
    }
    return PROMISES[inv["id"]]


def reconcile(items: list[dict]) -> None:
    """Update promise statuses from the latest invoice list."""
    by_id = {i["id"]: i for i in items}
    today = date.today()
    for p in list(PROMISES.values()):
        inv = by_id.get(p["invoice_id"])
        if inv is None:
            continue
        if inv["status"] in _PAID:
            if p["status"] == "active":
                p["status"] = "kept"
                _event(f"Promise kept: {p['client']} paid invoice {p['number']}")
            elif p["status"] == "broken":
                p["status"] = "paid_late"
                _event(f"{p['client']} paid invoice {p['number']} after the promised date")
        elif inv["status"] in _CLOSED:
            p["status"] = "closed"
        elif p["status"] == "active" and date.fromisoformat(p["promised_date"]) < today:
            p["status"] = "broken"
            _event(f"Promise broken: {p['client']} did not pay invoice {p['number']} by {p['promised_date']}")


def _find(invoice_id: str) -> dict | None:
    return next((i for i in pp.list_invoices("all") if i["id"] == invoice_id), None)


def _extract_json(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


class ParseIn(BaseModel):
    invoice_id: str
    reply: str = Field(min_length=3, max_length=1500)


class SaveIn(BaseModel):
    invoice_id: str
    promised_date: str
    note: str = Field(default="", max_length=300)


@router.get("")
def list_promises():
    return sorted(PROMISES.values(), key=lambda p: p["created"], reverse=True)


@router.post("/parse")
def parse(body: ParseIn):
    from . import agent  # imported here to avoid a circular import

    inv = _find(body.invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found.")
    today = date.today()
    system = (
        f"Today is {today.strftime('%A')}, {today.isoformat()}. A client replied to a payment reminder. "
        "Find out when they promise to pay. Return ONLY a JSON object, with no other text: "
        '{"promised_date": "YYYY-MM-DD" or null, "summary": "one short sentence", '
        '"intent": "will_pay" or "dispute" or "no_commitment"}. '
        "Resolve relative dates such as 'Friday', 'next week', 'in 3 days' or 'the 15th' from today's date. "
        "The reply may be English, Hindi or Hinglish. If the client gives no specific time, promised_date is null. "
        "Never invent a date."
    )
    try:
        resp = agent.client.chat.completions.create(
            model=agent.MODEL,
            max_tokens=1200,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": body.reply}],
        )
    except Exception as e:
        raise HTTPException(500, f"AI error: {e}")

    data = _extract_json(resp.choices[0].message.content or "") or {}
    parsed = None
    raw = data.get("promised_date")
    if isinstance(raw, str):
        try:
            d = date.fromisoformat(raw[:10])
            low = today - timedelta(days=60) if ALLOW_PAST else today
            if low <= d <= today + timedelta(days=120):
                parsed = d.isoformat()
        except ValueError:
            pass
    return {
        "invoice_number": inv["number"],
        "client": inv["client"],
        "promised_date": parsed,
        "summary": str(data.get("summary") or "Could not find a clear payment date.")[:200],
        "intent": str(data.get("intent") or "no_commitment"),
    }


@router.post("")
def create(body: SaveIn):
    inv = _find(body.invoice_id)
    if not inv:
        raise HTTPException(404, "Invoice not found.")
    try:
        return save(inv, body.promised_date, body.note)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/{invoice_id}")
def remove(invoice_id: str):
    PROMISES.pop(invoice_id, None)
    return {"ok": True}