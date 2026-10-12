"""MCP server: lets any AI agent use the invoice tools, with hard limits enforced here."""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from mcp.server.fastmcp import FastMCP  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app import paypal_client as pp  # noqa: E402
from app.models import InvoiceRequest  # noqa: E402

MAX_AMOUNT = float(os.getenv("MCP_MAX_INVOICE_AMOUNT", "200"))
MAX_ACTIONS_PER_DAY = int(os.getenv("MCP_MAX_ACTIONS_PER_DAY", "10"))
REMINDER_COOLDOWN_HOURS = 24
AUDIT_FILE = ROOT / "mcp_audit.log"
UNPAID = ("SENT", "UNPAID", "PARTIALLY_PAID", "PAYMENT_PENDING")
PAID = ("PAID", "MARKED_AS_PAID")

mcp = FastMCP("invoice-copilot")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _audit(action: str, status: str, detail: dict) -> None:
    rec = {"time": _now().isoformat(), "action": action, "status": status, "detail": detail}
    try:
        with AUDIT_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def _read_audit() -> list[dict]:
    if not AUDIT_FILE.exists():
        return []
    out = []
    for line in AUDIT_FILE.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _actions_today() -> int:
    today = _now().date().isoformat()
    return sum(1 for r in _read_audit() if r["status"] == "executed" and r["time"].startswith(today))


def _reminded_recently(invoice_id: str) -> bool:
    cutoff = _now() - timedelta(hours=REMINDER_COOLDOWN_HOURS)
    for r in _read_audit():
        if (
            r["action"] == "send_reminder"
            and r["status"] == "executed"
            and r["detail"].get("invoice_id") == invoice_id
            and datetime.fromisoformat(r["time"]) > cutoff
        ):
            return True
    return False


def _blocked(action: str, reason: str, detail: dict) -> dict:
    _audit(action, "blocked", {**detail, "reason": reason})
    return {
        "status": "blocked",
        "reason": reason,
        "next_step": "Ask the human owner to do this in the Invoice Copilot web app, where they can review and confirm it.",
    }


@mcp.tool()
def list_invoices(status_filter: str = "all") -> list[dict]:
    """List the freelancer's PayPal invoices with id, number, client, amount, status, due date and overdue flag.
    status_filter is one of: all, paid, unpaid, overdue."""
    if status_filter not in ("all", "paid", "unpaid", "overdue"):
        status_filter = "all"
    return pp.list_invoices(status_filter)


@mcp.tool()
def get_cash_flow_summary() -> dict:
    """Totals for invoiced, paid, outstanding and overdue amounts, plus the clients who owe the most."""
    items = pp.list_invoices("all")

    def amt(i):
        return float(i.get("amount") or 0)

    unpaid = [i for i in items if i["status"] in UNPAID]
    owed: dict[str, float] = {}
    for i in unpaid:
        owed[i["client"]] = owed.get(i["client"], 0) + amt(i)
    return {
        "invoices": len(items),
        "total_invoiced": round(sum(map(amt, items)), 2),
        "paid": round(sum(amt(i) for i in items if i["status"] in PAID), 2),
        "outstanding": round(sum(map(amt, unpaid)), 2),
        "overdue": round(sum(amt(i) for i in unpaid if i["overdue"]), 2),
        "most_owed": dict(sorted(owed.items(), key=lambda kv: -kv[1])[:3]),
    }


@mcp.tool()
def create_invoice(
    client_name: str,
    client_email: str,
    description: str,
    amount: float,
    currency: str = "USD",
    due_in_days: int = 14,
) -> dict:
    """Create and send a PayPal invoice. Guardrails: amounts above the configured cap are refused and
    need a human, and there is a daily limit on actions. Supported currencies: USD, EUR, GBP, AUD, CAD."""
    detail = {"client": client_name, "amount": amount, "currency": currency}
    try:
        req = InvoiceRequest(
            client_name=client_name,
            client_email=client_email,
            description=description,
            amount=Decimal(str(amount)),
            currency=currency,
            due_in_days=due_in_days,
        )
    except ValidationError as e:
        return {"status": "invalid", "errors": [f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()]}
    if float(req.amount) > MAX_AMOUNT:
        return _blocked("create_invoice", f"Amount is above the agent limit of {MAX_AMOUNT:.0f}.", detail)
    if _actions_today() >= MAX_ACTIONS_PER_DAY:
        return _blocked("create_invoice", "Daily action limit reached.", detail)
    try:
        res = pp.create_and_send_invoice(req)
    except Exception as e:
        _audit("create_invoice", "error", {**detail, "error": str(e)[:200]})
        return {"status": "error", "message": str(e)[:300]}
    _audit("create_invoice", "executed", {**detail, "invoice_number": res["invoice_number"]})
    return {
        "status": "created",
        "invoice_number": res["invoice_number"],
        "invoice_id": res["invoice_id"],
        "pay_url": res.get("pay_url"),
    }


@mcp.tool()
def send_reminder(invoice_id: str, message: str, subject: str = "Payment reminder") -> dict:
    """Send a payment reminder for an unpaid invoice (use the invoice id from list_invoices).
    Guardrails: only unpaid invoices, one reminder per invoice per 24 hours, a daily action limit,
    and no placeholder text such as [Name]."""
    detail = {"invoice_id": invoice_id}
    key = invoice_id.strip().lstrip("#")
    inv = next((i for i in pp.list_invoices("all") if key in (i["id"], i["number"])), None)
    if not inv:
        return {"status": "invalid", "message": "Invoice not found. Use an id from list_invoices."}
    detail = {"invoice_id": inv["id"], "number": inv["number"], "client": inv["client"]}
    if inv["status"] not in UNPAID:
        return _blocked("send_reminder", "That invoice is not awaiting payment.", detail)
    if not (10 <= len(message) <= 600):
        return {"status": "invalid", "message": "The message must be between 10 and 600 characters."}
    if re.search(r"\[[^\]]*\]", message):
        return {"status": "invalid", "message": "Remove placeholder text such as [Name] from the message."}
    if _reminded_recently(inv["id"]):
        return _blocked("send_reminder", "A reminder was already sent for this invoice in the last 24 hours.", detail)
    if _actions_today() >= MAX_ACTIONS_PER_DAY:
        return _blocked("send_reminder", "Daily action limit reached.", detail)
    try:
        pp.send_reminder(inv["id"], subject[:100], message)
    except Exception as e:
        _audit("send_reminder", "error", {**detail, "error": str(e)[:200]})
        return {"status": "error", "message": str(e)[:300]}
    _audit("send_reminder", "executed", detail)
    return {"status": "sent", "invoice_number": inv["number"]}


@mcp.tool()
def get_audit_log(limit: int = 10) -> list[dict]:
    """Show the most recent actions taken or blocked by agents through this server."""
    return _read_audit()[-max(1, min(limit, 50)):][::-1]


if __name__ == "__main__":
    mcp.run()