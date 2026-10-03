"""Seed data for the simulated company "Northwind Traders".

Everything is deterministic so runs are reproducible and tests can assert on
exact values. `fresh_state()` returns a deep copy the app mutates in memory.
"""

from __future__ import annotations

import copy

ERP_USERNAME = "agent.worker"
ERP_PASSWORD = "nw-demo-2026"
ERP_API_KEY = "nw-test-api-key-7f3a"

VENDORS = [
    {"id": "V-1001", "name": "Acme Cloud Services", "email": "billing@acmecloud.example"},
    {"id": "V-1002", "name": "Globex Corporation", "email": "ar@globex.example"},
    {"id": "V-1003", "name": "Globex Logistics Ltd", "email": "dispatch@globexlogistics.example"},
    {"id": "V-1004", "name": "Initech Supplies", "email": "orders@initech.example"},
    {"id": "V-1005", "name": "Umbrella Facilities", "email": "accounts@umbrella.example"},
]

CATEGORIES = ["Software & Cloud", "Logistics", "Office Supplies", "Facilities", "Professional Services"]

# Invoices exist as PDF attachments on emails. Amounts are strings so the PDF
# shows exactly what a human would see (thousands separators, currency).
INVOICES = {
    "ACME-2026-0917": {
        "vendor": "Acme Cloud Services",
        "vendor_address": "500 Market St, San Francisco, CA",
        "number": "ACME-2026-0917",
        "issue_date": "2026-09-30",
        "due_date": "2026-10-30",
        "currency": "USD",
        "lines": [
            ("Compute - reserved instances (Sep)", 1, "3,600.00"),
            ("Object storage 18.2 TB", 1, "812.50"),
            ("Premium support", 1, "400.00"),
        ],
        "total": "4,812.50",
        "po": "PO-88123",
    },
    "ACME-2026-0831": {
        "vendor": "Acme Cloud Services",
        "vendor_address": "500 Market St, San Francisco, CA",
        "number": "ACME-2026-0831",
        "issue_date": "2026-08-31",
        "due_date": "2026-09-30",
        "currency": "USD",
        "lines": [("Compute - on demand (Aug)", 1, "1,040.00"), ("Premium support", 1, "200.00")],
        "total": "1,240.00",
        "po": "PO-88123",
    },
    "GX-55120": {
        "vendor": "Globex Corporation",
        "vendor_address": "1 Globex Plaza, Cypress Creek",
        "number": "GX-55120",
        "issue_date": "2026-09-25",
        "due_date": "2026-10-25",
        "currency": "EUR",
        "lines": [("Consulting - data migration, 10 days", 10, "215.00")],
        "total": "2,150.00",
        "po": "PO-88140",
    },
    "GL-7781": {
        "vendor": "Globex Logistics Ltd",
        "vendor_address": "Unit 4, Dockside Park, Felixstowe",
        "number": "GL-7781",
        "issue_date": "2026-09-14",
        "due_date": "2026-10-14",
        "currency": "GBP",
        "lines": [("Freight - 3 pallets LHR->RTM", 3, "286.80")],
        "total": "860.40",
        "po": "PO-88101",
    },
    "IS-3302": {
        "vendor": "Initech Supplies",
        "vendor_address": "4120 Freidrich Ln, Austin, TX",
        "number": "IS-3302",
        "issue_date": "2026-09-11",
        "due_date": "2026-10-11",
        "currency": "USD",
        "lines": [("Printer toner (x6)", 6, "49.99"), ("Copy paper, 10 reams", 1, "90.05")],
        "total": "389.99",
        "po": None,
    },
}

EMAILS = [
    {
        "id": "m-108",
        "from_name": "Acme Cloud Billing",
        "from_addr": "billing@acmecloud.example",
        "subject": "Your Acme Cloud invoice ACME-2026-0917 is ready",
        "date": "2026-10-02 09:14",
        "body": (
            "Hello Northwind Traders,\n\n"
            "Your invoice for September 2026 is attached.\n"
            "Please reference the invoice number with your payment.\n\n"
            "Thanks,\nAcme Cloud Billing"
        ),
        "attachments": ["ACME-2026-0917"],
    },
    {
        "id": "m-107",
        "from_name": "Acme Cloud",
        "from_addr": "news@acmecloud.example",
        "subject": "Introducing Acme Edge Functions - 30% off this month",
        "date": "2026-09-29 16:02",
        "body": "Deploy closer to your users. Use code EDGE30 for 30% off your first month.",
        "attachments": [],
    },
    {
        "id": "m-106",
        "from_name": "Globex Accounts Receivable",
        "from_addr": "ar@globex.example",
        "subject": "Invoice GX-55120 - data migration consulting",
        "date": "2026-09-26 11:40",
        "body": "Dear customer,\n\nPlease find invoice GX-55120 attached.\n\nKind regards,\nGlobex AR",
        "attachments": ["GX-55120"],
    },
    {
        "id": "m-105",
        "from_name": "Acme Cloud Billing",
        "from_addr": "billing@acmecloud.example",
        "subject": "Reminder: invoice ACME-2026-0831 is due on 30 Sep",
        "date": "2026-09-20 08:00",
        "body": (
            "This is a friendly reminder that invoice ACME-2026-0831 for USD 1,240.00 "
            "is due on 2026-09-30.\n\nAcme Cloud Billing"
        ),
        "attachments": [],
    },
    {
        "id": "m-104",
        "from_name": "Globex Logistics",
        "from_addr": "dispatch@globexlogistics.example",
        "subject": "Globex Logistics invoice GL-7781",
        "date": "2026-09-15 14:22",
        "body": "Hi,\n\nInvoice for last week's freight is attached.\n\nGlobex Logistics Ltd",
        "attachments": ["GL-7781"],
    },
    {
        "id": "m-103",
        "from_name": "Priya Raman (CFO)",
        "from_addr": "cfo@northwind.example",
        "subject": "Reminder: AP process",
        "date": "2026-09-12 10:05",
        "body": (
            "Team,\n\nReminder that every vendor invoice must be entered as a bill in the ERP "
            "within 5 business days of receipt, using the category that matches the vendor.\n"
            "Bills over USD 10,000 need my sign-off before entry.\n\nPriya"
        ),
        "attachments": [],
    },
    {
        "id": "m-102",
        "from_name": "Initech Supplies",
        "from_addr": "orders@initech.example",
        "subject": "Initech invoice IS-3302",
        "date": "2026-09-11 17:45",
        "body": "Thanks for your order! Invoice attached.\n\nInitech Supplies",
        "attachments": ["IS-3302"],
    },
    {
        "id": "m-101",
        "from_name": "Acme Cloud Billing",
        "from_addr": "billing@acmecloud.example",
        "subject": "Your Acme Cloud invoice ACME-2026-0831 is ready",
        "date": "2026-09-01 09:10",
        "body": "Hello Northwind Traders,\n\nYour invoice for August 2026 is attached.\n\nAcme Cloud Billing",
        "attachments": ["ACME-2026-0831"],
    },
]

# Bills already in the ERP. The August Acme invoice is already entered, so an
# agent that picks the wrong ("not latest") invoice hits a duplicate error.
BILLS = [
    {
        "id": "AP-1041",
        "vendor_id": "V-1001",
        "invoice_number": "ACME-2026-0831",
        "invoice_date": "2026-08-31",
        "due_date": "2026-09-30",
        "currency": "USD",
        "amount": 1240.00,
        "category": "Software & Cloud",
        "status": "Paid",
        "notes": "",
        "created_by": "j.doe",
    },
    {
        "id": "AP-1042",
        "vendor_id": "V-1003",
        "invoice_number": "GL-7781",
        "invoice_date": "2026-09-14",
        "due_date": "2026-10-14",
        "currency": "GBP",
        "amount": 860.40,
        "category": "Logistics",
        "status": "Open",
        "notes": "",
        "created_by": "j.doe",
    },
]


def fresh_state() -> dict:
    return {
        "vendors": copy.deepcopy(VENDORS),
        "bills": copy.deepcopy(BILLS),
        "emails": copy.deepcopy(EMAILS),
        "sent": [],
        "next_bill": 1043,
        "sessions": {},  # token -> {"user": str, "views": int}
        "chaos": {"flaky_submit": False, "session_timeout_after": 0},
        "chaos_fired": set(),
    }
