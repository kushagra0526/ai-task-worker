"""Render a vendor invoice as a real PDF so the worker has to download and parse it."""

from __future__ import annotations

from fpdf import FPDF


def render_invoice_pdf(inv: dict) -> bytes:
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 10, inv["vendor"], new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, inv["vendor_address"], new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, "INVOICE", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    meta = [
        ("Invoice number", inv["number"]),
        ("Invoice date", inv["issue_date"]),
        ("Payment due", inv["due_date"]),
        ("Bill to", "Northwind Traders, 12 Harbour Rd, Seattle, WA"),
    ]
    if inv.get("po"):
        meta.append(("PO reference", inv["po"]))
    for label, value in meta:
        pdf.cell(45, 7, f"{label}:")
        pdf.cell(0, 7, value, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(110, 8, "Description", border=1)
    pdf.cell(20, 8, "Qty", border=1, align="R")
    pdf.cell(50, 8, f"Unit price ({inv['currency']})", border=1, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    for desc, qty, unit in inv["lines"]:
        pdf.cell(110, 8, desc, border=1)
        pdf.cell(20, 8, str(qty), border=1, align="R")
        pdf.cell(50, 8, unit, border=1, align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(130, 9, "Total due", align="R")
    pdf.cell(50, 9, f"{inv['currency']} {inv['total']}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(8)
    pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(0, 5, "Please quote the invoice number on your remittance. Thank you for your business.")
    return bytes(pdf.output())
