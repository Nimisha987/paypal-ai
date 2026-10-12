import os
from types import SimpleNamespace as NS

from dotenv import load_dotenv

load_dotenv()
from app import paypal_client as pp  # noqa: E402

buyer = os.environ["SANDBOX_BUYER_EMAIL"]
demo = [
    ("Rahul Sharma", "Logo design", 400, 10),
    ("Priya Verma", "Website copywriting", 250, 5),
    ("Amit Singh", "Social media banners", 120, 3),
    ("Neha Kapoor", "Brand guidelines", 900, 30),
]
for name, desc, amount, days in demo:
    inv = NS(client_name=name, client_email=buyer, description=desc,
             amount=amount, currency="USD", due_in_days=days)
    res = pp.create_and_send_invoice(inv)
    print(res["invoice_number"], name, res["pay_url"])