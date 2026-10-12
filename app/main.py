from dotenv import load_dotenv

load_dotenv()  # must run before the other imports read the environment

import asyncio
import io
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import segno
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, autopilot, promises
from . import paypal_client as pp
from .webhooks import EVENTS, router as webhook_router

BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(autopilot.loop())
    yield
    task.cancel()


app = FastAPI(title="Invoice Copilot", lifespan=lifespan)
app.include_router(webhook_router)
app.include_router(autopilot.router)
app.include_router(promises.router)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


class ChatIn(BaseModel):
    session_id: str
    message: str


@app.get("/")
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.post("/api/chat")
def chat(body: ChatIn):
    try:
        reply, pending = agent.run_agent(body.session_id, body.message)
    except Exception as e:
        raise HTTPException(500, f"Agent error: {e}")
    return {"reply": reply, "pending": pending}


@app.post("/api/confirm/{pid}")
def confirm(pid: str):
    try:
        return agent.confirm(pid)
    except KeyError:
        raise HTTPException(404, "This action has expired. Ask the copilot again.")
    except Exception as e:
        raise HTTPException(502, str(e))


@app.post("/api/cancel/{pid}")
def cancel(pid: str):
    agent.cancel(pid)
    return {"ok": True}


# ---- Status-change tracking (works without a webhook) ----
_seen: dict[str, str] = {}
_first_load = True
_LABELS = {
    "PAID": "paid",
    "MARKED_AS_PAID": "marked as paid",
    "PARTIALLY_PAID": "partially paid",
    "CANCELLED": "cancelled",
    "REFUNDED": "refunded",
}


def _track_changes(items: list[dict]) -> None:
    global _first_load
    for i in items:
        old, new = _seen.get(i["id"]), i["status"]
        if not _first_load and old is not None and old != new and new in _LABELS:
            EVENTS.append(
                {
                    "time": datetime.now(timezone.utc).isoformat(),
                    "type": "STATUS_CHANGE",
                    "summary": f"Invoice {i['number']} ({i['client']}) was {_LABELS[new]}",
                }
            )
        _seen[i["id"]] = new
    _first_load = False
    del EVENTS[:-50]


@app.get("/api/invoices")
def invoices():
    try:
        items = pp.list_invoices("all")
        _track_changes(items)
        promises.reconcile(items)
        return items
    except Exception as e:
        raise HTTPException(502, str(e))


@app.get("/api/events")
def events():
    return EVENTS[-20:][::-1]


# ---- QR code for payment links ----
@app.get("/api/qr")
def qr_code(url: str):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if len(url) > 500 or parsed.scheme != "https" or not (host == "paypal.com" or host.endswith(".paypal.com")):
        raise HTTPException(400, "Only PayPal payment links are supported.")
    buf = io.BytesIO()
    segno.make(url, error="m").save(buf, kind="svg", scale=5, border=2)
    return Response(buf.getvalue(), media_type="image/svg+xml")


# ---- AI cash-flow summary ----
_PAID = ("PAID", "MARKED_AS_PAID")
_UNPAID = ("SENT", "UNPAID", "PARTIALLY_PAID", "PAYMENT_PENDING")
_summary_cache: dict = {"key": None, "text": None}


def _summary_stats(items: list[dict]) -> dict:
    def amt(i):
        return float(i.get("amount") or 0)

    paid = [i for i in items if i["status"] in _PAID]
    unpaid = [i for i in items if i["status"] in _UNPAID]
    overdue = [i for i in unpaid if i["overdue"]]
    owed: dict[str, float] = {}
    for i in unpaid:
        owed[i["client"]] = owed.get(i["client"], 0) + amt(i)
    today = date.today()
    late_days = []
    for i in overdue:
        if i.get("due_date"):
            late_days.append((today - date.fromisoformat(i["due_date"])).days)
    return {
        "currency": items[0]["currency"] if items else "USD",
        "invoices": len(items),
        "total_invoiced": round(sum(map(amt, items)), 2),
        "paid_total": round(sum(map(amt, paid)), 2),
        "outstanding_total": round(sum(map(amt, unpaid)), 2),
        "overdue_total": round(sum(map(amt, overdue)), 2),
        "overdue_count": len(overdue),
        "oldest_overdue_days": max(late_days, default=0),
        "owed_by_client": dict(sorted(owed.items(), key=lambda kv: -kv[1])[:5]),
    }


@app.post("/api/insights/summary")
def insights_summary():
    try:
        stats = _summary_stats(pp.list_invoices("all"))
    except Exception as e:
        raise HTTPException(502, str(e))
    if stats["invoices"] == 0:
        return {"summary": "No invoices yet. Create one to see insights."}

    key = json.dumps(stats, sort_keys=True)
    if _summary_cache["key"] == key and _summary_cache["text"]:
        return {"summary": _summary_cache["text"]}

    try:
        resp = agent.client.chat.completions.create(
            model=agent.MODEL,
            max_tokens=800,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a finance assistant for a freelancer. From the JSON data, write exactly 3 "
                        "short sentences in plain text (no markdown): 1) how cash flow looks, 2) the biggest "
                        "risk, 3) one specific action to take. Use only the numbers provided."
                    ),
                },
                {"role": "user", "content": key},
            ],
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        raise HTTPException(500, f"AI error: {e}")

    if not text:
        text = "Could not generate a summary right now. Please try again."
    else:
        _summary_cache.update({"key": key, "text": text})
    return {"summary": text}