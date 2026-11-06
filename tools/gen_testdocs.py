#!/usr/bin/env python3
"""Generate deterministic, entirely fictional test documents (and invalid inputs) for Document Desk.

    python tools/gen_testdocs.py OUTPUT_DIR

All parties, numbers and identifiers are invented. Requires the `dev` extra (reportlab, Pillow)."""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

from reportlab.lib.pagesizes import LETTER
from reportlab.pdfgen import canvas

FOOTER = "FICTIONAL SAMPLE - generated for software testing - not a real document"


def pdf(path: Path, title: str, lines: list[str], pages: list[list[str]] | None = None) -> None:
    c = canvas.Canvas(str(path), pagesize=LETTER, invariant=1)  # invariant=1 -> reproducible bytes
    c.setTitle(title)
    for chunk in [lines, *(pages or [])]:
        y = 740
        c.setFont("Helvetica-Bold", 16)
        c.drawString(72, y, title)
        c.setFont("Helvetica", 11)
        y -= 34
        for ln in chunk:
            for part in _wrap(ln, 95):
                c.drawString(72, y, part)
                y -= 16
        c.setFont("Helvetica-Oblique", 8)
        c.drawString(72, 40, FOOTER)
        c.showPage()
    c.save()


def _wrap(s: str, width: int) -> list[str]:
    words, out, cur = s.split(" "), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width and cur:
            out.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return out + [cur]


def tiff(path: Path, lines: list[str]) -> None:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("L", (1700, 2200), 255)
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=34)
    except TypeError:
        font = ImageFont.load_default()
    y = 140
    for ln in lines:
        d.text((120, y), ln, fill=0, font=font)
        y += 56
    img.save(path, format="TIFF", compression="tiff_lzw", dpi=(200, 200))


INVOICE = [
    "Invoice No: INV-2026-0042", "Invoice Date: 2026-03-14", "Due Date: 2026-04-13",
    "From: Northwind Fictional Supplies LLC", "Bill To: Contoso Demo Cafe", "Payment Terms: Net 30", "",
    "Item                         Qty    Unit Price    Amount",
    "Espresso beans 1kg            10        18.50     185.00",
    "Paper cups (case of 500)       4        42.00     168.00",
    "Delivery                       1        25.00      25.00", "",
    "Subtotal: 378.00", "Tax: 30.24", "Total: USD 408.24", "Amount Due: 408.24",
]
NDA = [
    "This Mutual Non-Disclosure Agreement is entered into by the parties below.",
    "Parties: Fabrikam Example Ltd and Adventure Works Sample Inc.",
    "Effective Date: 2026-02-01", "Governing Law: State of Delaware, USA",
    "Confidentiality Term: Obligations of confidentiality continue for three (3) years from the Effective Date.",
    "1. Purpose. The parties wish to explore a possible business relationship (the Purpose).",
    "2. Confidential Information means non-public information disclosed by either party in connection with the Purpose.",
    "3. Permitted use. The receiving party shall use Confidential Information only for the Purpose.",
    "4. Exclusions. Information that is public, independently developed or lawfully received from a third party is excluded.",
]
SOW = [
    "Statement of Work No. SOW-007 under the Master Service Agreement dated 2025-09-01 between the parties.",
    "Parties: Tailspin Demo Studio and Litware Fictional Corp.", "Effective Date: 2026-04-01",
    "Scope: Design and build a customer feedback portal for Litware Fictional Corp.",
    "Deliverables: Requirements workshop; Clickable prototype; Production release",
    "Milestone 1: Requirements workshop complete - 2026-04-15",
    "Milestone 2: Prototype accepted - 2026-05-20", "Milestone 3: Production release - 2026-07-01",
    "Total Fees: USD 48,000.00", "Term: 4 months from the Effective Date.",
]
MSA = [
    "Master Service Agreement", "Parties: Wingtip Sample Services and Proseware Fictional Holdings.",
    "Effective Date: 2025-09-01", "Term: Three years, renewing annually unless terminated.",
    "Governing Law: State of Washington, USA",
    "Individual projects will be described in separate Statements of Work issued under this agreement.",
    "Termination: Either party may terminate on 60 days written notice.",
    "Limitation of liability: Neither party is liable for indirect or consequential damages.",
]
PO = [
    "PO Number: PO-55012", "Order Date: 2026-05-02", "Vendor: Alpine Ski House Fictional Outfitters",
    "Bill To: Coho Winery Demo", "Item                         Qty    Unit Price",
    "Wine crates                  200        3.10", "Cork stoppers (box)         50       12.00",
    "Total: 1220.00", "Delivery terms: Deliver to warehouse 2 within 14 days.",
]
RECEIPT = [
    "Merchant: Lakeside Fictional Hardware", "Date: 2026-06-09", "Receipt No: R-88231",
    "Hammer                       1        14.99", "Wood screws (100)           2         6.50",
    "Subtotal: 27.99", "Tax: 2.24", "Total: 30.23", "Paid by: Visa ending in 0000 (fictional)",
]
BANK = [
    "Bank Statement", "Institution: First Fictional Credit Union", "Statement Date: 2026-06-30",
    "Period Start: 2026-06-01", "Period End: 2026-06-30", "Account: ****0000 (sample)",
    "Opening Balance: 5,210.40", "Closing Balance: 6,018.15",
    "2026-06-03  Deposit - sample customer payment        1,200.00",
    "2026-06-15  Withdrawal - sample utilities             -392.25",
]
OTHER = [
    "Community Bake Sale", "Saturday 14 June, 10am - 2pm at the Fictional Park pavilion.",
    "Cakes, cookies and lemonade. All proceeds go to the sample library fund.", "Everyone welcome!",
]


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    pdf(out / "invoice_northwind.pdf", "INVOICE", INVOICE)
    pdf(out / "nda_fabrikam.pdf", "MUTUAL NON-DISCLOSURE AGREEMENT", NDA)
    pdf(out / "sow_tailspin.pdf", "STATEMENT OF WORK", SOW)
    pdf(out / "msa_wingtip.pdf", "MASTER SERVICE AGREEMENT", MSA)
    pdf(out / "purchase_order_alpine.pdf", "PURCHASE ORDER", PO)
    pdf(out / "receipt_lakeside.pdf", "RECEIPT", RECEIPT)
    pdf(out / "bank_statement_ffcu.pdf", "BANK STATEMENT", BANK)
    pdf(out / "other_bake_sale_flyer.pdf", "Bake Sale", OTHER)
    pdf(out / "multipage_invoice.pdf", "INVOICE", INVOICE, pages=[["Page 2: terms and conditions (fictional)."] * 6])
    tiff(out / "invoice_scan.tif", ["INVOICE", *INVOICE])
    # invalid / unsupported inputs
    (out / "invalid_text_renamed.pdf").write_text("This is plain text, not a PDF.\n")
    with zipfile.ZipFile(out / "unsupported.docx", "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
    (out / "empty.pdf").write_bytes(b"")
    (out / "corrupt_header_only.pdf").write_bytes(b"%PDF-1.7\n%truncated fictional file\n")
    with (out / "oversized.pdf").open("wb") as f:  # 21 MiB: above the default 20 MiB limit
        f.write(b"%PDF-1.4\n")
        f.write(b"0" * (21 * 1024 * 1024))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(Path(sys.argv[1]))
    print(f"wrote test documents to {sys.argv[1]}")
