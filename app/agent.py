import json
import os
import uuid
from datetime import date

from openai import OpenAI
from pydantic import ValidationError

from . import paypal_client as pp
from . import promises
from .models import InvoiceRequest

client = OpenAI(
    api_key=os.environ["LLM_API_KEY"],
    base_url=os.environ["LLM_BASE_URL"],
    max_retries=2,
    timeout=30,
)
MODEL = os.environ["LLM_MODEL"]

SESSIONS: dict[str, list] = {}   # conversation history per browser session
PENDING: dict[str, dict] = {}    # actions waiting for user confirmation


def system_prompt() -> str:
    today = date.today()
    return f"""You are Invoice Copilot, an assistant that helps freelancers get paid using PayPal.
Today is {today.strftime('%A')}, {today.isoformat()}.

Rules:
- Use tools for anything about invoices, payments or disputes. Never invent data.
- To create an invoice you need: client name, client email, description, amount. Default currency is USD and default due period is 14 days. If a required detail is missing, ask for it.
- create_invoice, send_reminder and record_promise only PREPARE an action. The user must confirm it in the interface. Never say something was done until the user confirms.
- For overdue invoices, offer a reminder and match the tone the user asks for (friendly or firm).
- For send_reminder and record_promise, call list_invoices first and use the invoice "id" value (it starts with INV2-), not the short invoice number.
- When the user says a client promised to pay by a date, call record_promise with an exact YYYY-MM-DD date worked out from today's date. If no date is given, ask for one. While a promise is active, Autopilot sends no reminders for that invoice.
- Never leave placeholders like [Your Name] in a message. Sign reminders simply with "Best regards" and no name.
- For analytics questions (best week, who owes me, totals), call list_invoices or search_transactions and calculate carefully.
- For disputes, explain in plain language what the buyer claims and what evidence helps, then draft a short professional seller response.
- Say each thing only once. Do not repeat sentences.
- Reply in plain text with no markdown. Keep answers short and clear.

Language:
- You understand English, Hindi (Devanagari) and Hinglish (Hindi written in English letters). Understand amounts like "5k", "paanch hazaar" or "400 dollar".
- Reply in the same language and script the user wrote in. If they write Hinglish, reply in Hinglish using English letters.
- Anything sent to a client (invoice descriptions, reminder text) stays in English unless the user asks for another language.
- If the user mentions rupees, Rs, INR or the rupee sign, explain that this sandbox supports only USD, EUR, GBP, AUD and CAD, and ask which currency and amount to use. Do not convert currencies yourself."""


def _tool(name: str, description: str, properties: dict, required: list | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
            },
        },
    }


TOOLS = [
    _tool(
        "create_invoice",
        "Prepare a PayPal invoice for the user to confirm. Does not send it.",
        {
            "client_name": {"type": "string"},
            "client_email": {"type": "string"},
            "description": {"type": "string", "description": "What the invoice is for"},
            "amount": {"type": "number"},
            "currency": {"type": "string", "enum": ["USD", "EUR", "GBP", "AUD", "CAD"]},
            "due_in_days": {"type": "integer"},
        },
        ["client_name", "client_email", "description", "amount"],
    ),
    _tool(
        "list_invoices",
        "List the user's invoices with id, number, status, amount, client and due date.",
        {"status_filter": {"type": "string", "enum": ["all", "paid", "unpaid", "overdue"]}},
    ),
    _tool(
        "search_transactions",
        "Search PayPal transactions in a date range (max 31 days). May lag behind recent activity.",
        {
            "start_date": {"type": "string", "description": "YYYY-MM-DD"},
            "end_date": {"type": "string", "description": "YYYY-MM-DD"},
        },
        ["start_date", "end_date"],
    ),
    _tool(
        "send_reminder",
        "Prepare a payment reminder for an invoice. The user must confirm before it is sent.",
        {
            "invoice_id": {"type": "string", "description": "Invoice id from list_invoices"},
            "subject": {"type": "string"},
            "note": {"type": "string", "description": "The reminder message to the client"},
        },
        ["invoice_id", "subject", "note"],
    ),
    _tool(
        "record_promise",
        "Prepare to save a client's promise to pay an invoice by a date. The user must confirm. While the promise is active, Autopilot will not send reminders for that invoice.",
        {
            "invoice_id": {"type": "string", "description": "Invoice id from list_invoices"},
            "promised_date": {"type": "string", "description": "YYYY-MM-DD"},
            "note": {"type": "string", "description": "What the client said, in one short sentence"},
        },
        ["invoice_id", "promised_date"],
    ),
    _tool(
        "get_disputes",
        "List open disputes, or get details of one dispute by id.",
        {"dispute_id": {"type": "string"}},
    ),
]


def _find_invoice(key) -> dict | None:
    key = str(key).strip().lstrip("#")
    return next(
        (
            i
            for i in pp.list_invoices("all")
            if key in (i["id"], i["number"])
            or (key.isdigit() and str(i["number"]).isdigit() and int(key) == int(i["number"]))
        ),
        None,
    )


def _stash(kind: str, data: dict, session_id: str, title: str, details: list) -> dict:
    pid = uuid.uuid4().hex[:8]
    PENDING[pid] = {"kind": kind, "data": data, "session": session_id}
    return {"id": pid, "title": title, "details": details}


def run_tool(name: str, args: dict, session_id: str, pending: list) -> object:
    try:
        if name == "create_invoice":
            try:
                req = InvoiceRequest(**args)
            except ValidationError as e:
                return {"error": "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors())}
            pending.append(
                _stash(
                    "create_invoice",
                    req.model_dump(mode="json"),
                    session_id,
                    "Review invoice",
                    [
                        ["Client", req.client_name],
                        ["Email", req.client_email],
                        ["For", req.description],
                        ["Amount", f"{req.amount:.2f} {req.currency}"],
                        ["Due", f"in {req.due_in_days} days"],
                    ],
                )
            )
            return {"status": "awaiting_user_confirmation"}

        if name == "send_reminder":
            match = _find_invoice(args.get("invoice_id", ""))
            if not match:
                return {"error": f"No invoice found for '{args.get('invoice_id')}'. Use the id from list_invoices."}
            pending.append(
                _stash(
                    "send_reminder",
                    {"invoice_id": match["id"], "subject": args["subject"], "note": args["note"]},
                    session_id,
                    "Review reminder",
                    [
                        ["Invoice", f"{match['number']} ({match['client']})"],
                        ["Subject", args["subject"]],
                        ["Message", args["note"]],
                    ],
                )
            )
            return {"status": "awaiting_user_confirmation"}

        if name == "record_promise":
            match = _find_invoice(args.get("invoice_id", ""))
            if not match:
                return {"error": f"No invoice found for '{args.get('invoice_id')}'. Use the id from list_invoices."}
            if match["status"] in ("PAID", "MARKED_AS_PAID"):
                return {"error": "That invoice is already paid."}
            try:
                d = date.fromisoformat(str(args.get("promised_date", ""))[:10])
            except ValueError:
                return {"error": "promised_date must be in YYYY-MM-DD format."}
            if d < date.today() and not promises.ALLOW_PAST:
                return {"error": "That date is in the past. Ask the user for a future date."}
            note = str(args.get("note", ""))[:300]
            pending.append(
                _stash(
                    "record_promise",
                    {"invoice_id": match["id"], "promised_date": d.isoformat(), "note": note},
                    session_id,
                    "Save promise to pay",
                    [
                        ["Invoice", f"{match['number']} ({match['client']})"],
                        ["Promised by", d.isoformat()],
                        ["Note", note or "-"],
                    ],
                )
            )
            return {"status": "awaiting_user_confirmation"}

        if name == "list_invoices":
            return pp.list_invoices(args.get("status_filter", "all"))
        if name == "search_transactions":
            return pp.search_transactions(args["start_date"], args["end_date"])
        if name == "get_disputes":
            return pp.get_disputes(args.get("dispute_id"))
        return {"error": f"Unknown tool {name}"}
    except Exception as e:  # return errors to the model so it can explain them
        return {"error": str(e)}


def run_agent(session_id: str, user_text: str):
    history = SESSIONS.setdefault(session_id, [])
    start = len(history)
    history.append({"role": "user", "content": user_text})
    pending: list = []
    try:
        for _ in range(6):
            resp = client.chat.completions.create(
                model=MODEL,
                max_tokens=1024,
                tools=TOOLS,
                messages=[{"role": "system", "content": system_prompt()}] + history,
            )
            msg = resp.choices[0].message

            if not msg.tool_calls:
                text = msg.content or ""
                history.append({"role": "assistant", "content": text})
                return text, pending

            history.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments or "{}",
                            },
                        }
                        for tc in msg.tool_calls
                    ],
                }
            )
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                out = run_tool(tc.function.name, args, session_id, pending)
                history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(out, default=str)[:12000],
                    }
                )
        return "I couldn't finish that request. Please try rephrasing it.", pending
    except Exception:
        del history[start:]  # avoid leaving a half-finished tool exchange in history
        raise


def confirm(pid: str) -> dict:
    item = PENDING.pop(pid, None)
    if item is None:
        raise KeyError(pid)
    link = None
    kind, data = item["kind"], item["data"]
    if kind == "create_invoice":
        res = pp.create_and_send_invoice(InvoiceRequest(**data))
        msg = f"Invoice {res['invoice_number']} for {data['client_name']} was created and sent."
        link = res.get("pay_url")
    elif kind == "send_reminder":
        pp.send_reminder(**data)
        msg = "Reminder sent."
    else:  # record_promise
        inv = _find_invoice(data["invoice_id"])
        if not inv:
            raise RuntimeError("Invoice not found.")
        promises.save(inv, data["promised_date"], data.get("note", ""))
        msg = f"Saved. Autopilot will hold reminders for invoice {inv['number']} until {data['promised_date']}."
    SESSIONS.setdefault(item["session"], []).append(
        {"role": "user", "content": f"[System note: the user confirmed the action. {msg}]"}
    )
    return {"message": msg, "link": link}


def cancel(pid: str) -> None:
    item = PENDING.pop(pid, None)
    if item:
        SESSIONS.setdefault(item["session"], []).append(
            {"role": "user", "content": "[System note: the user cancelled the pending action.]"}
        )