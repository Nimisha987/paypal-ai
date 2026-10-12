# Invoice Copilot

**An AI agent that helps freelancers get paid: it creates PayPal invoices from plain language, understands what clients promise, chases late payments on its own, and never moves money without a human in control.**

Built for the *Build What's Next with PayPal and AI* hackathon.

- **Live demo:** <YOUR-RENDER-URL>
- **Demo video:** <YOUR-YOUTUBE-LINK>
- **Repository:** <YOUR-REPO-URL>

> Everything runs on the **PayPal Sandbox**. No real money is involved.

---

## The problem

Freelancers lose hours every week writing invoices, remembering who has not paid, and writing awkward reminder messages. Foreign clients are often slow to pay, and the follow-up work is repetitive and uncomfortable.

## The solution

Invoice Copilot puts an AI agent in front of the PayPal Invoicing API. You describe what you need in English, Hindi or Hinglish, and the agent turns it into real PayPal actions. Anything that sends something to a client is confirmed by a human first. An optional **Autopilot** watches for overdue invoices and drafts reminders whose tone gets firmer the later an invoice is. When a client replies with a promise ("I'll pay by Friday"), the AI reads it, and Autopilot holds back until that date. If the promise is broken, the next reminder says so. An **MCP server** lets other AI agents use the same tools, with hard limits enforced server-side.

## Features

| Feature | What it does |
|---|---|
| **Chat to invoice** | "Invoice Rahul Sharma, rahul@example.com, $400 for logo design, due in 10 days" becomes a real PayPal invoice. The agent asks for missing details. |
| **Confirm before sending** | Every invoice, reminder and saved promise is shown on a review card. Nothing happens until you click confirm. |
| **Payments Q&A** | Ask "Who hasn't paid me yet?" or "How much am I owed?". The agent reads live data from PayPal. |
| **Smart reminders** | AI-written reminders that match the tone you ask for (friendly, firm, final). |
| **Promise-to-pay tracker** | Paste a client's reply, or tell the chat what they said. The AI extracts the promised date (including relative dates and Hinglish). While a promise is active, Autopilot sends no reminders for that invoice. If the date passes unpaid, the promise is marked broken and the next reminder mentions it with a firmer tone. Kept and broken promises show up in the activity feed. |
| **Autopilot with guardrails** | Checks overdue invoices every 30 seconds, drafts a reminder with a tone based on how late the invoice is, and either asks for approval or sends automatically within a limit you set. Includes an audit log. |
| **MCP server** | Exposes invoice tools to any MCP-compatible agent (for example Claude Desktop) with a per-invoice amount cap, a daily action cap, a reminder cooldown and an audit log. |
| **Insights** | Collection rate, outstanding amount, a weekly chart, top outstanding clients and an AI-written cash-flow summary. |
| **Hindi and Hinglish** | Understands "Rahul ko 400 dollar ka invoice bhejo" and replies in the same language. Voice input supports English and Hindi. |
| **Voice input** | Speak an invoice using the microphone button (Chrome and Edge). |
| **WhatsApp share and QR code** | Share a payment link on WhatsApp, or show a QR code the client can scan to pay. |
| **Live payment updates** | PayPal webhooks, plus a polling fallback that works without a public URL. |

## How PayPal is used

PayPal is central to every action in the app. All calls are made against the sandbox.

| PayPal capability | Used for |
|---|---|
| **Invoicing API** (generate invoice number, create, send, get) | Creating and sending invoices from the chat and from MCP agents |
| **Invoicing API** (list) | Live invoice table, stats, overdue detection, promise tracking, and the data the agent reasons over |
| **Invoicing API** (remind) | Sending payment reminders, manual, from Autopilot and from MCP agents |
| **Webhooks** (`INVOICING.INVOICE.PAID`, `CANCELLED`, `REFUNDED`) | Real-time "Activity" feed |
| **Webhook signature verification** (`verify-webhook-signature`) | Rejects fake webhook calls |
| **Transaction Search API** | Available to the agent for transaction history questions |
| **Disputes API** | Available to the agent for listing and explaining disputes |
| **OAuth 2.0** (client credentials) | Authenticating every request, with token caching |

## How AI is used

- **Tool-calling agent.** The model decides which tool to call (`create_invoice`, `list_invoices`, `search_transactions`, `send_reminder`, `record_promise`, `get_disputes`), extracts structured arguments from natural language, and explains results back to the user.
- **Human-in-the-loop by design.** `create_invoice`, `send_reminder` and `record_promise` only *prepare* an action. The backend executes it only after the user confirms in the UI.
- **Validated output.** Everything the model extracts (name, email, amount, currency, dates) is validated before PayPal is called or a promise is stored. The model's numbers are never trusted blindly.
- **Reading client replies.** The model turns a free-text reply (English, Hindi or Hinglish) into a structured promise: a date, a one-line summary and an intent (will pay, dispute, or no commitment). Dates outside a sensible range are rejected.
- **Autopilot.** The AI drafts each reminder using the client's name, amount, days overdue and any broken promise, and the tone is chosen from the invoice's lateness. A template fallback is used if the model is unavailable.
- **Insights.** The model turns computed statistics into a short plain-language summary. It only sees aggregate numbers.
- **Multilingual.** The agent understands English, Hindi and Hinglish and answers in the user's language. Messages sent to clients stay in English unless asked otherwise.

## Safety and guardrails

- Money-related actions in the web app always need confirmation, unless Autopilot is explicitly switched to automatic mode.
- Autopilot limits: at most 3 reminders per check, a cooldown between reminders for the same invoice, a configurable auto-send amount limit (larger amounts always need approval), and an audit log of every decision.
- MCP limits (enforced inside the server, not by the calling agent): invoices above `MCP_MAX_INVOICE_AMOUNT` (default 200) are refused, at most `MCP_MAX_ACTIONS_PER_DAY` (default 10) actions per day, one reminder per invoice per 24 hours, no reminders on paid invoices, no placeholder text, and every action or refusal is written to `mcp_audit.log`.
- Webhook signatures are verified when `PAYPAL_WEBHOOK_ID` is set.
- The QR endpoint only accepts HTTPS links on `paypal.com`.
- Secrets live in environment variables and are never committed.

## Architecture

```
Browser (single-page UI, plain HTML/JS)           Other AI agents (MCP clients)
        |                                                   |
        v                                                   v
FastAPI backend (app/main.py)                     MCP server (app/mcp_server.py)
   |-- agent.py          LLM tool-calling loop, confirmation flow        |
   |-- promises.py       promise parsing, tracking, kept/broken status   |
   |-- autopilot.py      background loop, approval queue, guardrails     |
   |-- paypal_client.py  OAuth, invoices, reminders, transactions, disputes, webhook verification
   |-- webhooks.py       PayPal webhook receiver
   |-- models.py         Pydantic validation
        |                       |
        v                       v
   PayPal Sandbox API      LLM (OpenAI-compatible API)
```

## Tools and technologies

- **PayPal Sandbox:** Invoicing API, Webhooks, Transaction Search, Disputes, OAuth
- **AI model:** `openai/gpt-oss-120b` served by **Groq** through its OpenAI-compatible API (tool calling, Hinglish understanding, reading client replies, reminder drafting, summaries). The provider is configurable: any OpenAI-compatible endpoint works by changing three environment variables.
- **Model Context Protocol:** official `mcp` Python SDK (FastMCP) for the agent-facing server
- **Backend:** Python, FastAPI, Uvicorn, httpx, Pydantic
- **Frontend:** vanilla HTML, CSS and JavaScript, Web Speech API for voice
- **Other libraries:** `segno` (QR codes), `python-dotenv`
- **Hosting:** Render

## Run it yourself

Requirements: **Python 3.10 or newer**.

### 1. PayPal sandbox setup

1. Create an account at [developer.paypal.com](https://developer.paypal.com) and make sure the **Sandbox** toggle is on.
2. Under **Testing Tools, Sandbox Accounts**, create a **Business** account and a **Personal** account. Use country **United States**, because the Invoicing API may not be available for some countries.
3. Under **Apps & Credentials**, create a **Merchant** app linked to that Business account. Enable **Invoicing** (plus **Transaction Search** and **Disputes** if offered) and save.
4. Copy the **Client ID** and **Secret**.

### 2. Get a free LLM key

Create a key with any OpenAI-compatible provider. The free tier of Groq (console.groq.com/keys) works well. Model names change often, so check the provider's current model list if a name is rejected.

### 3. Install and configure

```bash
git clone <YOUR-REPO-URL>
cd <YOUR-REPO-FOLDER>
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # Windows: copy .env.example .env
```

Fill in `.env`:

| Variable | Description |
|---|---|
| `PAYPAL_CLIENT_ID` | Sandbox app Client ID |
| `PAYPAL_CLIENT_SECRET` | Sandbox app Secret |
| `PAYPAL_BASE_URL` | `https://api-m.sandbox.paypal.com` |
| `PAYPAL_WEBHOOK_ID` | Optional. Only needed for webhook signature verification |
| `SANDBOX_BUYER_EMAIL` | Email of your sandbox **Personal** account (used by the seed script) |
| `LLM_API_KEY` | Your LLM provider key |
| `LLM_BASE_URL` | e.g. `https://api.groq.com/openai/v1` |
| `LLM_MODEL` | e.g. `openai/gpt-oss-120b` |
| `MCP_MAX_INVOICE_AMOUNT` | Optional. Largest invoice an MCP agent may create (default 200) |
| `MCP_MAX_ACTIONS_PER_DAY` | Optional. Daily limit for MCP actions (default 10) |
| `PROMISE_DEMO_MODE` | Optional. Set to `1` to allow promise dates in the past, so the broken-promise flow can be shown without waiting |

### 4. Check the setup

```bash
python -m scripts.check_setup
```

### 5. Start the app

```bash
uvicorn app.main:app --reload
```

Open http://localhost:8000.

### 6. Optional: demo data

```bash
python -m scripts.seed_sandbox
```

Creates a few sample invoices addressed to your sandbox buyer email.

### 7. Optional: PayPal webhooks

Webhooks need a public URL.

1. Expose your app (for example `ngrok http 8000`) or use your hosted URL.
2. In the PayPal dashboard, open your app, click **Add Webhook**, and enter `https://YOUR-URL/webhooks/paypal`.
3. Under **Invoicing invoice**, subscribe to **Invoice paid** (and optionally cancelled and refunded).
4. Put the **Webhook ID** into `PAYPAL_WEBHOOK_ID` and restart.

Without a webhook, the app still detects status changes by polling PayPal every 5 seconds, so the demo works locally.

## Use it from another AI agent (MCP)

The MCP server runs separately from the web app and talks to the same PayPal sandbox.

Quick test with the MCP Inspector (needs Node.js):

```bash
npx @modelcontextprotocol/inspector python app/mcp_server.py
```

Claude Desktop: add this to `claude_desktop_config.json` (use your own paths) and restart it.

```json
{
  "mcpServers": {
    "invoice-copilot": {
      "command": "C:\\path\\to\\project\\venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\project\\app\\mcp_server.py"]
    }
  }
}
```

Tools: `list_invoices`, `get_cash_flow_summary`, `create_invoice`, `send_reminder`, `get_audit_log`. Try asking the agent for a $500 invoice: the server refuses it and tells the agent to ask the human owner.

## Deploy on Render

1. Create a **Web Service** from this repository.
2. **Build command:** `pip install -r requirements.txt`
3. **Start command:** `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Add all variables from `.env` under **Environment**.
5. Update the PayPal webhook URL to your Render URL and set `PAYPAL_WEBHOOK_ID`.

## Try it in three minutes

1. Open the app and go to **Assistant**. Send:
   `Invoice Rahul Sharma, <YOUR-SANDBOX-BUYER-EMAIL>, $50 for logo design, due in 7 days`
2. Review the card and click **Confirm and send**.
3. Open **Invoices**, expand the invoice, click **Open payment page**, and pay with your sandbox **Personal** account (use a private window).
4. Watch the invoice switch to **Paid** and the event appear under **Activity**.
5. Ask: `Who hasn't paid me yet?` or try Hinglish: `Priya ke invoice ka friendly reminder likho`.
6. Expand an overdue invoice, paste `Sorry for the delay, I will pay by Friday` under "Did the client promise to pay?", click **Understand reply**, then **Save promise**.
7. Open **Autopilot**, turn it on, click **Run check now**, and see the audit log say it is holding off. Approve a drafted reminder for another overdue invoice.
8. Open **Insights** and click **Generate AI summary**.

## Project structure

```
app/
  main.py            FastAPI app, routes, status tracking, insights summary, QR endpoint
  agent.py           LLM tool-calling agent and confirmation flow
  promises.py        Promise-to-pay parsing, storage and kept/broken tracking
  autopilot.py       Autopilot loop, approval queue and audit log
  mcp_server.py      MCP server with server-side guardrails
  paypal_client.py   PayPal REST client
  webhooks.py        Webhook receiver
  models.py          Validation schemas
  static/index.html  Single-page UI
scripts/
  seed_sandbox.py    Creates demo invoices
  check_setup.py     Verifies configuration and features
.env.example         Environment variable template
requirements.txt
LICENSE
```

## Known limitations

- **Sandbox only.** Built and tested against the PayPal Sandbox.
- **State is in memory.** Chat history, the activity feed, promises, and Autopilot settings, queue and log reset when the web server restarts. A database would be the next step.
- **MCP server is a separate process.** It does not share the web app's in-memory state. Its guardrails use their own audit file (`mcp_audit.log`), and it cannot show confirmation cards, so it refuses anything above its caps instead.
- **Client replies are pasted by hand.** The sandbox does not receive real client email, so promises are logged from pasted text or from the chat.
- **Currencies.** The sandbox accounts used here support USD, EUR, GBP, AUD and CAD. INR was not available, and the assistant says so instead of converting.
- **Disputes and Transaction Search.** These may return empty results or errors in the sandbox, and Transaction Search can lag behind real activity. The assistant reports this instead of inventing data. Live invoice data comes from the Invoicing API.
- **Insights chart.** PayPal's invoice list does not include payment dates, so the weekly chart groups by invoice date.
- **Hosting.** On free hosting tiers the app may sleep when idle, which also pauses Autopilot until it wakes up.
- **Voice input** relies on the browser's speech recognition (Chrome and Edge) and needs an internet connection.

## License

MIT. See [LICENSE](LICENSE).