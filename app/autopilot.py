import asyncio
import threading
import uuid
from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import agent, promises
from . import paypal_client as pp

router = APIRouter(prefix="/api/autopilot")

SETTINGS = {
    "enabled": False,
    "mode": "ask",            # "ask" = approval queue, "auto" = send within limits
    "min_days_overdue": 1,
    "cooldown_days": 3,
    "auto_limit": 500.0,
    "max_per_run": 3,
}
QUEUE: dict[str, dict] = {}
LOG: list[dict] = []
LAST_REMINDED: dict[str, datetime] = {}
FAILED: dict[str, datetime] = {}
_state = {"last_run": None}
_scan_lock = threading.Lock()


def _log(kind: str, text: str) -> None:
    LOG.append({"time": datetime.now(timezone.utc).isoformat(), "kind": kind, "text": text})
    del LOG[:-60]


def _tone(days: int) -> str:
    if days <= 7:
        return "friendly"
    if days <= 21:
        return "firm but polite"
    return "firm and final"


def _first_name(client: str | None) -> str:
    if not client or "@" in client:
        return "there"
    return client.split()[0]


def _fallback(inv: dict, days: int, tone: str, broken: dict | None) -> str:
    first = _first_name(inv.get("client"))
    amount = f"{float(inv.get('amount') or 0):.2f} {inv.get('currency') or ''}".strip()
    when = f"{days} day{'s' if days != 1 else ''}"
    if tone == "friendly":
        body = (f"A quick reminder that invoice {inv['number']} for {amount} was due {when} ago. "
                "If you have already paid, please ignore this note. Otherwise you can pay securely using the link in the invoice.")
    elif tone == "firm but polite":
        body = (f"Invoice {inv['number']} for {amount} is now {when} overdue. "
                "Please arrange payment using the link in the invoice as soon as possible, or let me know if there is a problem.")
    else:
        body = (f"Invoice {inv['number']} for {amount} is {when} overdue and still unpaid. "
                "Please pay it today using the link in the invoice. If I do not hear from you, I will have to follow up further.")
    if broken:
        body += f" You had mentioned you would pay by {broken['promised_date']}."
    return f"Hi {first},\n\n{body}\n\nBest regards"


def _compose(inv: dict, days: int, tone: str, broken: dict | None) -> tuple[str, str]:
    subject = f"Payment reminder: invoice {inv['number']}"
    text = ""
    try:
        details = (
            f"Client first name: {_first_name(inv.get('client'))}\n"
            f"Invoice number: {inv['number']}\n"
            f"Amount: {float(inv.get('amount') or 0):.2f} {inv.get('currency') or ''}\n"
            f"Days overdue: {days}\n"
            f"Tone: {tone}"
        )
        if broken:
            details += f"\nBroken promise: the client said they would pay by {broken['promised_date']}"
        resp = agent.client.chat.completions.create(
            model=agent.MODEL,
            max_tokens=500,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Write a short payment reminder email body (max 70 words) from a freelancer to a client. "
                        "Use the requested tone. Mention the invoice number, the amount and how many days overdue. "
                        "If a broken promise is given, politely remind them of the date they promised. "
                        "Plain text only, no markdown, no subject line, no placeholders like [Name]. "
                        "Start with 'Hi <first name>,' and end with 'Best regards'."
                    ),
                },
                {"role": "user", "content": details},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception:
        text = ""
    if len(text) < 20:
        text = _fallback(inv, days, tone, broken)
    return subject, text[:900]


def scan(force_ask: bool = False) -> dict:
    """Look for overdue invoices and either send reminders or queue them for approval."""
    if not _scan_lock.acquire(blocking=False):
        return {"queued": 0, "sent": 0, "busy": True}
    try:
        s = SETTINGS
        now = datetime.now(timezone.utc)
        today = date.today()
        queued = sent = 0

        try:
            all_invoices = pp.list_invoices("all")
        except Exception as e:
            _log("error", f"Could not read invoices: {str(e)[:120]}")
            return {"queued": 0, "sent": 0}

        promises.reconcile(all_invoices)
        invoices = [i for i in all_invoices if i["overdue"]]

        current_ids = {i["id"] for i in invoices}
        for pid, q in list(QUEUE.items()):
            if q["invoice_id"] not in current_ids:
                QUEUE.pop(pid, None)
                _log("info", f"Invoice {q['number']} ({q['client']}) is no longer overdue. Removed from approvals.")
            elif promises.get_active(q["invoice_id"]):
                QUEUE.pop(pid, None)
                _log("info", f"A promise to pay was logged for invoice {q['number']} ({q['client']}). Removed from approvals.")
        queued_ids = {q["invoice_id"] for q in QUEUE.values()}

        for inv in invoices:
            if queued + sent >= s["max_per_run"]:
                break
            if not inv.get("due_date") or inv["id"] in queued_ids:
                continue

            promise = promises.PROMISES.get(inv["id"])
            broken = None
            if promise:
                if promise["status"] == "active":
                    if not promise.get("hold_logged"):
                        promise["hold_logged"] = True
                        _log("info", f"Holding off on invoice {inv['number']} ({inv['client']}): the client promised to pay by {promise['promised_date']}")
                    continue
                if promise["status"] == "broken":
                    broken = promise

            days = (today - date.fromisoformat(inv["due_date"])).days
            if days < s["min_days_overdue"]:
                continue
            last = LAST_REMINDED.get(inv["id"])
            if last and (now - last).total_seconds() < s["cooldown_days"] * 86400:
                continue
            failed = FAILED.get(inv["id"])
            if failed and (now - failed).total_seconds() < 1800:
                continue

            tone = _tone(days)
            if broken and tone == "friendly":
                tone = "firm but polite"
            subject, note = _compose(inv, days, tone, broken)
            amount = float(inv.get("amount") or 0)
            auto_ok = (not force_ask) and s["mode"] == "auto" and amount <= s["auto_limit"]
            label = f"invoice {inv['number']} ({inv['client']}), {days} days late"
            reason = f"Broken promise: they said they would pay by {broken['promised_date']}" if broken else ""
            if broken:
                label += ", broken promise"

            if auto_ok:
                try:
                    pp.send_reminder(inv["id"], subject, note)
                    LAST_REMINDED[inv["id"]] = now
                    sent += 1
                    _log("sent", f"Sent a {tone} reminder automatically for {label}")
                except Exception as e:
                    FAILED[inv["id"]] = now
                    _log("error", f"Could not send reminder for {label}: {str(e)[:120]}")
            else:
                pid = uuid.uuid4().hex[:8]
                QUEUE[pid] = {
                    "id": pid,
                    "invoice_id": inv["id"],
                    "number": inv["number"],
                    "client": inv["client"],
                    "amount": amount,
                    "currency": inv.get("currency") or "USD",
                    "days_late": days,
                    "tone": tone,
                    "reason": reason,
                    "subject": subject,
                    "note": note,
                    "created": now.isoformat(),
                }
                queued += 1
                if s["mode"] == "auto" and amount > s["auto_limit"] and not force_ask:
                    why = f"amount is above the auto-send limit of {s['auto_limit']:.0f}"
                else:
                    why = "waiting for your approval"
                _log("queued", f"Drafted a {tone} reminder for {label} ({why})")

        _state["last_run"] = now.isoformat()
        return {"queued": queued, "sent": sent}
    finally:
        _scan_lock.release()


async def loop() -> None:
    while True:
        await asyncio.sleep(30)
        if not SETTINGS["enabled"]:
            continue
        try:
            await asyncio.to_thread(scan)
        except Exception as e:
            _log("error", f"Autopilot check failed: {str(e)[:120]}")


class SettingsIn(BaseModel):
    enabled: bool | None = None
    mode: Literal["ask", "auto"] | None = None
    min_days_overdue: int | None = Field(default=None, ge=1, le=90)
    cooldown_days: int | None = Field(default=None, ge=1, le=60)
    auto_limit: float | None = Field(default=None, ge=0, le=100000)


@router.get("")
def state():
    return {
        "settings": SETTINGS,
        "queue": sorted(QUEUE.values(), key=lambda q: -q["days_late"]),
        "log": LOG[-30:][::-1],
        "last_run": _state["last_run"],
    }


@router.post("/settings")
def update_settings(body: SettingsIn):
    changes = body.model_dump(exclude_none=True)
    old = dict(SETTINGS)
    SETTINGS.update(changes)
    if "enabled" in changes and changes["enabled"] != old["enabled"]:
        _log("info", "Autopilot turned " + ("on" if changes["enabled"] else "off"))
    if "mode" in changes and changes["mode"] != old["mode"]:
        _log("info", "Mode: " + ("send automatically within limits" if changes["mode"] == "auto" else "ask before sending"))
    return SETTINGS


@router.post("/run")
def run_now():
    return scan(force_ask=not SETTINGS["enabled"])


@router.post("/approve/{pid}")
def approve(pid: str):
    q = QUEUE.get(pid)
    if not q:
        raise HTTPException(404, "This item is no longer in the queue.")
    try:
        pp.send_reminder(q["invoice_id"], q["subject"], q["note"])
    except Exception as e:
        _log("error", f"Could not send reminder for invoice {q['number']}: {str(e)[:120]}")
        raise HTTPException(502, str(e))
    QUEUE.pop(pid, None)
    LAST_REMINDED[q["invoice_id"]] = datetime.now(timezone.utc)
    _log("sent", f"You approved: reminder sent for invoice {q['number']} ({q['client']})")
    return {"ok": True}


@router.post("/dismiss/{pid}")
def dismiss(pid: str):
    q = QUEUE.pop(pid, None)
    if q:
        LAST_REMINDED[q["invoice_id"]] = datetime.now(timezone.utc)  # snooze for the cooldown period
        _log("info", f"You dismissed the reminder for invoice {q['number']} ({q['client']}). Snoozed.")
    return {"ok": True}