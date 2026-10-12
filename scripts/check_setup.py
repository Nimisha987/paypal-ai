"""Self-check: confirms the features listed in the README are wired up."""
import importlib
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

failed = []


def check(label, ok, hint=""):
    print(("PASS  " if ok else "FAIL  ") + label + ("" if ok else "   -> " + hint))
    if not ok:
        failed.append(label)


for var in ("PAYPAL_CLIENT_ID", "PAYPAL_CLIENT_SECRET", "PAYPAL_BASE_URL", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
    check(f"env var {var}", bool(os.getenv(var)), "add it to .env")

for pkg in ("fastapi", "httpx", "openai", "segno", "mcp", "pydantic", "dotenv"):
    try:
        importlib.import_module(pkg)
        check(f"package {pkg}", True)
    except ImportError:
        check(f"package {pkg}", False, "pip install -r requirements.txt")

for f in ("main.py", "agent.py", "paypal_client.py", "models.py", "webhooks.py",
          "autopilot.py", "promises.py", "mcp_server.py", "static/index.html"):
    check(f"file app/{f}", (ROOT / "app" / f).exists(), "create it from the guide")

try:
    from app.main import app

    paths = {getattr(r, "path", "") for r in app.routes}
    for p in ("/api/chat", "/api/confirm/{pid}", "/api/invoices", "/api/qr", "/api/autopilot",
              "/api/promises", "/api/promises/parse", "/api/insights/summary", "/webhooks/paypal"):
        check(f"route {p}", p in paths, "update app/main.py")
except Exception as e:
    check("app imports cleanly", False, str(e)[:200])

html_path = ROOT / "app" / "static" / "index.html"
html = html_path.read_text(encoding="utf-8") if html_path.exists() else ""
for marker, label in (
    ("Share on WhatsApp", "WhatsApp share"),
    ("/api/qr", "QR code"),
    ("hi-IN", "Hindi voice"),
    ("/api/autopilot", "Autopilot UI"),
    ("/api/insights/summary", "Insights UI"),
    ("/api/promises", "Promise-to-pay UI"),
):
    check(f"UI has {label}", marker in html, "update app/static/index.html")

try:
    from app import mcp_server

    check("MCP server loads", hasattr(mcp_server, "mcp"), "see app/mcp_server.py")
except Exception as e:
    check("MCP server loads", False, str(e)[:200])

print("\nAll checks passed." if not failed else f"\n{len(failed)} check(s) failed.")
sys.exit(1 if failed else 0)