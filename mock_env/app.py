"""Northwind Traders: a small simulated company environment.

Three surfaces the worker can use, all served from one process:

* /mail  - a webmail inbox with vendor invoices as PDF attachments
* /erp   - an internal accounts-payable app (login, bills, vendors)
* /erp/api - a JSON API for the same ERP (API-key auth)

Fault injection ("chaos") is switched on via /_admin/chaos so the worker's
failure handling can be exercised deterministically:

* flaky_submit: the first successful bill submission is committed but the
  user sees a 502 page - an *ambiguous* failure, where blindly retrying
  creates a duplicate.
* session_timeout_after=N: the ERP session expires once after N page views.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import data
from .pdf import render_invoice_pdf

app = FastAPI(title="Northwind Traders (simulated)")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
STATE = data.fresh_state()

SESSION_COOKIE = "nw_session"
DATE_RE = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")


# --------------------------------------------------------------------------- helpers


def vendor_by_id(vendor_id: str) -> dict | None:
    return next((v for v in STATE["vendors"] if v["id"] == vendor_id), None)


def to_display_date(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")


def parse_display_date(value: str) -> str | None:
    """DD/MM/YYYY -> ISO, or None when malformed."""
    m = DATE_RE.match(value.strip())
    if not m:
        return None
    try:
        return datetime(int(m[3]), int(m[2]), int(m[1])).strftime("%Y-%m-%d")
    except ValueError:
        return None


def bill_view(bill: dict) -> dict:
    vendor = vendor_by_id(bill["vendor_id"]) or {"name": "?"}
    return {
        **bill,
        "vendor_name": vendor["name"],
        "invoice_date_display": to_display_date(bill["invoice_date"]),
        "due_date_display": to_display_date(bill["due_date"]),
        "amount_display": f"{bill['amount']:,.2f}",
    }


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def current_user(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE)
    session = STATE["sessions"].get(token or "")
    if not session:
        return None
    session["views"] += 1
    limit = STATE["chaos"]["session_timeout_after"]
    if limit and session["views"] > limit and "session" not in STATE["chaos_fired"]:
        STATE["chaos_fired"].add("session")
        STATE["sessions"].pop(token, None)
        return None
    return session["user"]


def login_redirect(request: Request, expired: bool) -> RedirectResponse:
    target = f"/erp/login?next={request.url.path}"
    if expired:
        target += "&expired=1"
    return RedirectResponse(target, status_code=303)


def validate_bill(fields: dict, *, iso_dates: bool) -> tuple[dict | None, list[str]]:
    """Shared validation for the HTML form and the JSON API."""
    errors: list[str] = []
    vendor = vendor_by_id(fields.get("vendor_id", ""))
    if not vendor:
        errors.append("Select a valid vendor.")
    number = (fields.get("invoice_number") or "").strip()
    if not number:
        errors.append("Invoice number is required.")

    dates = {}
    for key, label in (("invoice_date", "Invoice date"), ("due_date", "Due date")):
        raw = (fields.get(key) or "").strip()
        if iso_dates:
            try:
                dates[key] = datetime.strptime(raw, "%Y-%m-%d").strftime("%Y-%m-%d")
            except ValueError:
                errors.append(f"{label} must be an ISO date (YYYY-MM-DD).")
        else:
            iso = parse_display_date(raw)
            if iso is None:
                errors.append(f"{label} must be in DD/MM/YYYY format (e.g. 30/10/2026).")
            else:
                dates[key] = iso
    if len(dates) == 2 and dates["due_date"] < dates["invoice_date"]:
        errors.append("Due date cannot be before the invoice date.")

    raw_amount = str(fields.get("amount", "")).strip()
    if not AMOUNT_RE.match(raw_amount):
        errors.append("Amount must be a plain number without currency symbols or thousands separators, e.g. 1234.56.")
    elif float(raw_amount) <= 0:
        errors.append("Amount must be greater than zero.")

    currency = fields.get("currency", "")
    if currency not in ("USD", "EUR", "GBP"):
        errors.append("Currency must be USD, EUR or GBP.")
    category = fields.get("category", "")
    if category not in data.CATEGORIES:
        errors.append("Select a category.")

    if vendor and number:
        dup = next(
            (b for b in STATE["bills"] if b["vendor_id"] == vendor["id"] and b["invoice_number"].lower() == number.lower()),
            None,
        )
        if dup:
            errors.append(f"Duplicate: bill {dup['id']} already exists for this vendor and invoice number.")

    if errors:
        return None, errors
    return {
        "vendor_id": vendor["id"],
        "invoice_number": number,
        "invoice_date": dates["invoice_date"],
        "due_date": dates["due_date"],
        "currency": currency,
        "amount": round(float(raw_amount), 2),
        "category": category,
        "notes": (fields.get("notes") or "").strip(),
    }, []


def commit_bill(bill: dict, user: str) -> dict:
    bill = {**bill, "id": f"AP-{STATE['next_bill']}", "status": "Open", "created_by": user}
    STATE["next_bill"] += 1
    STATE["bills"].append(bill)
    return bill


# --------------------------------------------------------------------------- intranet + mail


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return render(request, "home.html")


@app.get("/mail", response_class=HTMLResponse)
def inbox(request: Request, q: str = ""):
    emails = STATE["emails"]
    if q:
        needle = q.lower()
        emails = [
            e for e in emails if needle in (e["subject"] + e["from_name"] + e["from_addr"] + e["body"]).lower()
        ]
    return render(request, "mail_inbox.html", emails=emails, q=q)


@app.get("/mail/m/{email_id}", response_class=HTMLResponse)
def read_email(request: Request, email_id: str):
    email = next((e for e in STATE["emails"] if e["id"] == email_id), None)
    if not email:
        raise HTTPException(404, "Message not found")
    return render(request, "mail_message.html", email=email)


@app.get("/mail/attachments/{invoice_id}.pdf")
def attachment(invoice_id: str):
    invoice = data.INVOICES.get(invoice_id)
    if not invoice:
        raise HTTPException(404, "Attachment not found")
    return Response(
        render_invoice_pdf(invoice),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{invoice_id}.pdf"'},
    )


@app.get("/mail/compose", response_class=HTMLResponse)
def compose(request: Request, to: str = "", subject: str = ""):
    return render(request, "mail_compose.html", to=to, subject=subject, body="", error=None)


@app.post("/mail/send")
def send_mail(request: Request, to: str = Form(""), subject: str = Form(""), body: str = Form("")):
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", to.strip()) or not subject.strip():
        return render(
            request, "mail_compose.html", 422, to=to, subject=subject, body=body,
            error="A valid recipient address and a subject are required.",
        )
    STATE["sent"].append({"id": f"s-{len(STATE['sent']) + 1}", "to": to.strip(), "subject": subject.strip(), "body": body})
    return RedirectResponse("/mail/sent?ok=1", status_code=303)


@app.get("/mail/sent", response_class=HTMLResponse)
def sent(request: Request, ok: int = 0):
    return render(request, "mail_sent.html", sent=list(reversed(STATE["sent"])), ok=ok)


# --------------------------------------------------------------------------- ERP (HTML)


@app.get("/erp/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/erp", expired: int = 0):
    return render(request, "erp_login.html", next=next, expired=expired, error=None)


@app.post("/erp/login")
def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/erp")):
    if username != data.ERP_USERNAME or password != data.ERP_PASSWORD:
        return render(request, "erp_login.html", 401, next=next, expired=0, error="Invalid username or password.")
    token = secrets.token_urlsafe(16)
    STATE["sessions"][token] = {"user": username, "views": 0}
    resp = RedirectResponse(next if next.startswith("/erp") else "/erp", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True)
    return resp


@app.post("/erp/logout")
def logout(request: Request):
    STATE["sessions"].pop(request.cookies.get(SESSION_COOKIE, ""), None)
    return RedirectResponse("/erp/login", status_code=303)


@app.get("/erp", response_class=HTMLResponse)
def dashboard(request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request, expired=bool(request.cookies.get(SESSION_COOKIE)))
    open_bills = [b for b in STATE["bills"] if b["status"] == "Open"]
    return render(
        request, "erp_dashboard.html", user=user,
        open_count=len(open_bills), bill_count=len(STATE["bills"]),
        recent=[bill_view(b) for b in reversed(STATE["bills"][-5:])],
    )


@app.get("/erp/bills", response_class=HTMLResponse)
def bills(request: Request, q: str = ""):
    user = current_user(request)
    if not user:
        return login_redirect(request, expired=bool(request.cookies.get(SESSION_COOKIE)))
    rows = [bill_view(b) for b in reversed(STATE["bills"])]
    if q:
        needle = q.lower()
        rows = [r for r in rows if needle in (r["invoice_number"] + r["vendor_name"] + r["id"]).lower()]
    return render(request, "erp_bills.html", user=user, bills=rows, q=q)


@app.get("/erp/bills/new", response_class=HTMLResponse)
def new_bill(request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request, expired=bool(request.cookies.get(SESSION_COOKIE)))
    return render(
        request, "erp_bill_form.html", user=user, vendors=STATE["vendors"],
        categories=data.CATEGORIES, values={}, errors=[],
    )


@app.post("/erp/bills")
async def create_bill(request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request, expired=True)
    fields = dict(await request.form())
    bill, errors = validate_bill(fields, iso_dates=False)
    if errors:
        return render(
            request, "erp_bill_form.html", 422, user=user, vendors=STATE["vendors"],
            categories=data.CATEGORIES, values=fields, errors=errors,
        )
    created = commit_bill(bill, user)
    if STATE["chaos"]["flaky_submit"] and "submit" not in STATE["chaos_fired"]:
        STATE["chaos_fired"].add("submit")
        # The write already happened; the user just never sees confirmation.
        return HTMLResponse(
            "<html><head><title>502 Bad Gateway</title></head><body><h1>502 Bad Gateway</h1>"
            "<p>The server did not respond in time. Your request may or may not have been processed.</p>"
            "<p><a href='/erp'>Return to dashboard</a></p></body></html>",
            status_code=502,
        )
    return RedirectResponse(f"/erp/bills/{created['id']}?created=1", status_code=303)


@app.get("/erp/bills/{bill_id}", response_class=HTMLResponse)
def bill_detail(request: Request, bill_id: str, created: int = 0):
    user = current_user(request)
    if not user:
        return login_redirect(request, expired=bool(request.cookies.get(SESSION_COOKIE)))
    bill = next((b for b in STATE["bills"] if b["id"] == bill_id), None)
    if not bill:
        raise HTTPException(404, "Bill not found")
    return render(request, "erp_bill_detail.html", user=user, bill=bill_view(bill), created=created)


@app.get("/erp/vendors", response_class=HTMLResponse)
def vendors(request: Request):
    user = current_user(request)
    if not user:
        return login_redirect(request, expired=bool(request.cookies.get(SESSION_COOKIE)))
    return render(request, "erp_vendors.html", user=user, vendors=STATE["vendors"])


# --------------------------------------------------------------------------- ERP (JSON API)


def require_api_key(key: str | None) -> None:
    if key != data.ERP_API_KEY:
        raise HTTPException(401, "Missing or invalid X-API-Key header")


@app.get("/erp/api/vendors")
def api_vendors(q: str = "", x_api_key: str | None = Header(None)):
    require_api_key(x_api_key)
    rows = STATE["vendors"]
    if q:
        rows = [v for v in rows if q.lower() in v["name"].lower()]
    return {"vendors": rows}


@app.get("/erp/api/bills")
def api_bills(invoice_number: str = "", vendor_id: str = "", x_api_key: str | None = Header(None)):
    require_api_key(x_api_key)
    rows = STATE["bills"]
    if invoice_number:
        rows = [b for b in rows if b["invoice_number"].lower() == invoice_number.lower()]
    if vendor_id:
        rows = [b for b in rows if b["vendor_id"] == vendor_id]
    return {"bills": rows}


@app.post("/erp/api/bills")
async def api_create_bill(request: Request, x_api_key: str | None = Header(None)):
    require_api_key(x_api_key)
    bill, errors = validate_bill(await request.json(), iso_dates=True)
    if errors:
        return JSONResponse({"errors": errors}, status_code=422)
    created = commit_bill(bill, "api")
    if STATE["chaos"]["flaky_submit"] and "submit" not in STATE["chaos_fired"]:
        STATE["chaos_fired"].add("submit")  # same fault as the web form: committed, but the caller sees a 502
        return JSONResponse({"error": "upstream timeout - the request may or may not have been processed"},
                            status_code=502)
    return JSONResponse({"bill": created}, status_code=201)


# --------------------------------------------------------------------------- test/admin hooks


@app.post("/_admin/reset")
def admin_reset():
    STATE.clear()
    STATE.update(data.fresh_state())
    return {"ok": True}


@app.post("/_admin/chaos")
async def admin_chaos(request: Request):
    STATE["chaos"].update(await request.json())
    STATE["chaos_fired"] = set()
    return {"chaos": STATE["chaos"]}


@app.get("/_admin/state")
def admin_state():
    return {"bills": STATE["bills"], "sent": STATE["sent"], "chaos": STATE["chaos"]}
