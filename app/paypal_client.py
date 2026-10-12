import os
import time
from datetime import date, timedelta
from decimal import Decimal

import httpx

_token = {"value": None, "exp": 0.0}


def _base() -> str:
    return os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com")


def _auth() -> str:
    if _token["value"] and time.time() < _token["exp"] - 60:
        return _token["value"]
    r = httpx.post(
        f"{_base()}/v1/oauth2/token",
        auth=(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]),
        data={"grant_type": "client_credentials"},
        timeout=30,
    )
    r.raise_for_status()
    d = r.json()
    _token["value"] = d["access_token"]
    _token["exp"] = time.time() + d["expires_in"]
    return _token["value"]


def _req(method: str, path: str, **kw):
    r = httpx.request(
        method,
        f"{_base()}{path}",
        headers={"Authorization": f"Bearer {_auth()}", "Content-Type": "application/json"},
        timeout=30,
        **kw,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"PayPal {r.status_code}: {r.text[:400]}")
    return r.json() if r.content else {}


def _money(value) -> str:
    return f"{Decimal(str(value)):.2f}"


# ---------- Invoicing ----------
def create_and_send_invoice(inv) -> dict:
    number = _req("POST", "/v2/invoicing/generate-next-invoice-number")["invoice_number"]
    today = date.today()
    due = today + timedelta(days=inv.due_in_days)
    inv_date = today if due >= today else due - timedelta(days=14)

    first, _, last = inv.client_name.strip().partition(" ")
    name = {"given_name": first}
    if last:
        name["surname"] = last

    body = {
        "detail": {
            "invoice_number": number,
            "invoice_date": inv_date.isoformat(),
            "currency_code": inv.currency,
            "payment_term": {"due_date": due.isoformat()},
        },
        "primary_recipients": [
            {"billing_info": {"name": name, "email_address": inv.client_email}}
        ],
        "items": [
            {
                "name": inv.description,
                "quantity": "1",
                "unit_amount": {"currency_code": inv.currency, "value": _money(inv.amount)},
            }
        ],
    }
    created = _req("POST", "/v2/invoicing/invoices", json=body)
    invoice_id = created["href"].rsplit("/", 1)[-1]
    _req("POST", f"/v2/invoicing/invoices/{invoice_id}/send", json={"send_to_recipient": True})

    full = _req("GET", f"/v2/invoicing/invoices/{invoice_id}")
    pay_url = full.get("detail", {}).get("metadata", {}).get("recipient_view_url")
    return {"invoice_id": invoice_id, "invoice_number": number, "pay_url": pay_url}


def list_invoices(status_filter: str = "all") -> list[dict]:
    data = _req(
        "GET",
        "/v2/invoicing/invoices",
        params={"page": 1, "page_size": 50, "total_required": "true"},
    )
    today = date.today().isoformat()
    out = []
    for i in data.get("items", []):
        d = i.get("detail", {})
        billing = (i.get("primary_recipients") or [{}])[0].get("billing_info", {})
        nm = billing.get("name", {})
        status = i.get("status", "")
        due = d.get("payment_term", {}).get("due_date")
        unpaid = status in ("SENT", "UNPAID", "PARTIALLY_PAID", "PAYMENT_PENDING")
        out.append(
            {
                "id": i.get("id"),
                "number": d.get("invoice_number"),
                "status": status,
                "client": " ".join(filter(None, [nm.get("given_name"), nm.get("surname")]))
                or billing.get("email_address"),
                "email": billing.get("email_address"),
                "amount": i.get("amount", {}).get("value"),
                "currency": i.get("amount", {}).get("currency_code"),
                "invoice_date": d.get("invoice_date"),
                "due_date": due,
                "overdue": bool(unpaid and due and due < today),
                "pay_url": d.get("metadata", {}).get("recipient_view_url"),
            }
        )
    if status_filter == "paid":
        out = [x for x in out if x["status"] in ("PAID", "MARKED_AS_PAID")]
    elif status_filter == "unpaid":
        out = [x for x in out if x["status"] in ("SENT", "UNPAID", "PARTIALLY_PAID", "PAYMENT_PENDING")]
    elif status_filter == "overdue":
        out = [x for x in out if x["overdue"]]
    return out


def send_reminder(invoice_id: str, subject: str, note: str) -> dict:
    return _req(
        "POST",
        f"/v2/invoicing/invoices/{invoice_id}/remind",
        json={"subject": subject, "note": note, "send_to_recipient": True},
    )


# ---------- Transactions ----------
def search_transactions(start_date: str, end_date: str) -> list[dict]:
    data = _req(
        "GET",
        "/v1/reporting/transactions",
        params={
            "start_date": f"{start_date}T00:00:00+0000",
            "end_date": f"{end_date}T23:59:59+0000",
            "fields": "transaction_info",
            "page_size": 100,
        },
    )
    out = []
    for t in data.get("transaction_details", []):
        info = t.get("transaction_info", {})
        amt = info.get("transaction_amount", {})
        out.append(
            {
                "id": info.get("transaction_id"),
                "date": info.get("transaction_initiation_date"),
                "amount": amt.get("value"),
                "currency": amt.get("currency_code"),
                "status": info.get("transaction_status"),
                "subject": info.get("transaction_subject"),
            }
        )
    return out


# ---------- Disputes ----------
def get_disputes(dispute_id: str | None = None):
    if dispute_id:
        return _req("GET", f"/v1/customer/disputes/{dispute_id}")
    return _req("GET", "/v1/customer/disputes", params={"page_size": 10}).get("items", [])


# ---------- Webhooks ----------
def verify_webhook(headers, event: dict) -> bool:
    webhook_id = os.getenv("PAYPAL_WEBHOOK_ID")
    if not webhook_id:
        return True  # local dev only: set PAYPAL_WEBHOOK_ID in production
    res = _req(
        "POST",
        "/v1/notifications/verify-webhook-signature",
        json={
            "auth_algo": headers.get("paypal-auth-algo"),
            "cert_url": headers.get("paypal-cert-url"),
            "transmission_id": headers.get("paypal-transmission-id"),
            "transmission_sig": headers.get("paypal-transmission-sig"),
            "transmission_time": headers.get("paypal-transmission-time"),
            "webhook_id": webhook_id,
            "webhook_event": event,
        },
    )
    return res.get("verification_status") == "SUCCESS"