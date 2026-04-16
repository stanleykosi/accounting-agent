from __future__ import annotations

import csv
import json
import shutil
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import xlsxwriter
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent
PACK_DIR = ROOT / "document-stress-pack"
SOURCE_DIR = PACK_DIR / "source-documents"
COA_DIR = PACK_DIR / "coa"

CLOSE_PERIOD_START = date(2026, 3, 1)
CLOSE_PERIOD_END = date(2026, 3, 31)


@dataclass(frozen=True, slots=True)
class TransactionLine:
    date: date
    description: str
    reference: str
    debit: Decimal
    credit: Decimal


INVOICE_FIELDS: dict[str, object] = {
    "vendor_name": "ACME Industrial Supplies",
    "vendor_address": "455 Harbor Point, Newark, NJ 07102",
    "vendor_tax_id": "US-TAX-ACME-8450",
    "customer_name": "Northwind Traders LLC",
    "customer_tax_id": "US-TAX-NWT-9023",
    "invoice_number": "INV-1048",
    "invoice_date": date(2026, 3, 8),
    "due_date": date(2026, 3, 15),
    "currency": "USD",
    "subtotal": Decimal("2250.00"),
    "tax_amount": Decimal("200.00"),
    "tax_rate": Decimal("0.0889"),
    "discount_amount": Decimal("0.00"),
    "total": Decimal("2450.00"),
    "payment_terms": "Net 7",
    "notes": "Office chairs and monitor arms for the finance team expansion.",
}

INVOICE_LINE_ITEMS: tuple[dict[str, object], ...] = (
    {
        "description": "Ergonomic Office Chair",
        "quantity": Decimal("5"),
        "unit_price": Decimal("350.00"),
        "amount": Decimal("1750.00"),
        "tax_amount": Decimal("155.56"),
    },
    {
        "description": "Monitor Arm",
        "quantity": Decimal("5"),
        "unit_price": Decimal("100.00"),
        "amount": Decimal("500.00"),
        "tax_amount": Decimal("44.44"),
    },
)

OUT_OF_PERIOD_INVOICE_FIELDS: dict[str, object] = {
    "vendor_name": "Harbor Legal Group",
    "vendor_address": "18 State Street, Boston, MA 02109",
    "vendor_tax_id": "US-TAX-HLG-2201",
    "customer_name": "Northwind Traders LLC",
    "customer_tax_id": "US-TAX-NWT-9023",
    "invoice_number": "INV-APR-2201",
    "invoice_date": date(2026, 4, 4),
    "due_date": date(2026, 4, 11),
    "currency": "USD",
    "subtotal": Decimal("1500.00"),
    "tax_amount": Decimal("0.00"),
    "tax_rate": Decimal("0.00"),
    "discount_amount": Decimal("0.00"),
    "total": Decimal("1500.00"),
    "payment_terms": "Net 7",
    "notes": "April legal advisory invoice that should fall outside the March close window.",
}

OUT_OF_PERIOD_INVOICE_LINE_ITEMS: tuple[dict[str, object], ...] = (
    {
        "description": "Legal advisory and policy memo",
        "quantity": Decimal("10"),
        "unit_price": Decimal("150.00"),
        "amount": Decimal("1500.00"),
        "tax_amount": Decimal("0.00"),
    },
)

PAYSLIP_FIELDS: dict[str, object] = {
    "employee_name": "Jordan Lee",
    "employee_id": "EMP-2047",
    "employer_name": "Northwind Traders LLC",
    "pay_period_start": date(2026, 3, 1),
    "pay_period_end": date(2026, 3, 31),
    "pay_date": date(2026, 3, 15),
    "basic_salary": Decimal("5000.00"),
    "allowances": Decimal("450.00"),
    "deductions": Decimal("1285.00"),
    "gross_pay": Decimal("5450.00"),
    "net_pay": Decimal("4165.00"),
    "currency": "USD",
    "paye_tax": Decimal("930.00"),
    "pension_contribution": Decimal("275.00"),
}

PAYSLIP_DEDUCTIONS: tuple[dict[str, object], ...] = (
    {"deduction_type": "PAYE Tax", "amount": Decimal("930.00")},
    {"deduction_type": "Pension", "amount": Decimal("275.00")},
    {"deduction_type": "Health Premium", "amount": Decimal("80.00")},
)

CONTRACT_FIELDS: dict[str, object] = {
    "contract_number": "MSA-2026-017",
    "contract_date": date(2026, 2, 20),
    "effective_date": date(2026, 3, 1),
    "expiration_date": date(2027, 2, 28),
    "party_a_name": "Northwind Traders LLC",
    "party_b_name": "Cedar Ridge Analytics Inc.",
    "contract_value": Decimal("48000.00"),
    "currency": "USD",
    "contract_type": "Software Implementation",
    "terms": (
        "Fixed fee billed monthly in arrears for data migration, dashboard setup, and staff "
        "training."
    ),
    "renewal_terms": (
        "Renews automatically for 12-month periods unless either party gives 30 days notice."
    ),
    "termination_terms": (
        "Either party may terminate for material breach if the breach is not cured within 15 days."
    ),
}

BANK_TRANSACTION_LINES: tuple[TransactionLine, ...] = (
    TransactionLine(
        date=date(2026, 3, 2),
        description="CLIENT DEPOSIT NORTHWIND WEST REGION",
        reference="DEP-3302",
        debit=Decimal("0.00"),
        credit=Decimal("12000.00"),
    ),
    TransactionLine(
        date=date(2026, 3, 8),
        description="ACME INDUSTRIAL SUPPLIES INV-1048 OFFICE CHAIRS",
        reference="INV-1048",
        debit=Decimal("2450.00"),
        credit=Decimal("0.00"),
    ),
    TransactionLine(
        date=date(2026, 3, 12),
        description="CUSTOMER DEPOSIT RETAINER",
        reference="DEP-3310",
        debit=Decimal("0.00"),
        credit=Decimal("6500.00"),
    ),
    TransactionLine(
        date=date(2026, 3, 15),
        description="SALARY JORDAN LEE MARCH",
        reference="PR-2026-03-JL",
        debit=Decimal("4165.00"),
        credit=Decimal("0.00"),
    ),
    TransactionLine(
        date=date(2026, 3, 18),
        description="CLOUD HOSTING SUBSCRIPTION",
        reference="BILL-8810",
        debit=Decimal("980.00"),
        credit=Decimal("0.00"),
    ),
    TransactionLine(
        date=date(2026, 3, 22),
        description="OFFICE CATERING TEAM TRAINING",
        reference="RCP-3301",
        debit=Decimal("1875.00"),
        credit=Decimal("0.00"),
    ),
    TransactionLine(
        date=date(2026, 3, 27),
        description="CUSTOMER DEPOSIT PROJECT DELTA",
        reference="DEP-3344",
        debit=Decimal("0.00"),
        credit=Decimal("8100.00"),
    ),
    TransactionLine(
        date=date(2026, 3, 30),
        description="ELECTRIC UTILITY PAYMENT",
        reference="UTIL-1033",
        debit=Decimal("525.00"),
        credit=Decimal("0.00"),
    ),
)

COA_ROWS: tuple[dict[str, object], ...] = (
    {
        "Account Number": "1000",
        "Account": "Assets",
        "Type": "asset",
        "Parent Account": "",
        "Postable": "false",
        "Active": "true",
        "Default Department": "",
        "Default Cost Center": "",
        "Project": "",
        "QuickBooks ID": "",
    },
    {
        "Account Number": "1010",
        "Account": "Operating Cash",
        "Type": "asset",
        "Parent Account": "1000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Finance",
        "Default Cost Center": "HQ",
        "Project": "",
        "QuickBooks ID": "QB-1010",
    },
    {
        "Account Number": "1020",
        "Account": "Accounts Receivable",
        "Type": "asset",
        "Parent Account": "1000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Finance",
        "Default Cost Center": "HQ",
        "Project": "",
        "QuickBooks ID": "QB-1020",
    },
    {
        "Account Number": "1030",
        "Account": "Inventory",
        "Type": "asset",
        "Parent Account": "1000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Operations",
        "Default Cost Center": "Warehouse",
        "Project": "",
        "QuickBooks ID": "QB-1030",
    },
    {
        "Account Number": "1040",
        "Account": "Prepaid Expenses",
        "Type": "asset",
        "Parent Account": "1000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Finance",
        "Default Cost Center": "HQ",
        "Project": "",
        "QuickBooks ID": "QB-1040",
    },
    {
        "Account Number": "1100",
        "Account": "Fixed Assets",
        "Type": "asset",
        "Parent Account": "1000",
        "Postable": "false",
        "Active": "true",
        "Default Department": "",
        "Default Cost Center": "",
        "Project": "",
        "QuickBooks ID": "QB-1100",
    },
    {
        "Account Number": "1110",
        "Account": "Furniture and Fixtures",
        "Type": "asset",
        "Parent Account": "1100",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Admin",
        "Default Cost Center": "HQ",
        "Project": "",
        "QuickBooks ID": "QB-1110",
    },
    {
        "Account Number": "1120",
        "Account": "Computer Equipment",
        "Type": "asset",
        "Parent Account": "1100",
        "Postable": "true",
        "Active": "true",
        "Default Department": "IT",
        "Default Cost Center": "HQ",
        "Project": "",
        "QuickBooks ID": "QB-1120",
    },
    {
        "Account Number": "2000",
        "Account": "Liabilities",
        "Type": "liability",
        "Parent Account": "",
        "Postable": "false",
        "Active": "true",
        "Default Department": "",
        "Default Cost Center": "",
        "Project": "",
        "QuickBooks ID": "",
    },
    {
        "Account Number": "2010",
        "Account": "Accounts Payable",
        "Type": "liability",
        "Parent Account": "2000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Finance",
        "Default Cost Center": "HQ",
        "Project": "",
        "QuickBooks ID": "QB-2010",
    },
    {
        "Account Number": "2020",
        "Account": "Accrued Expenses",
        "Type": "liability",
        "Parent Account": "2000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Finance",
        "Default Cost Center": "HQ",
        "Project": "",
        "QuickBooks ID": "QB-2020",
    },
    {
        "Account Number": "2030",
        "Account": "Credit Card Payable",
        "Type": "liability",
        "Parent Account": "2000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Finance",
        "Default Cost Center": "HQ",
        "Project": "",
        "QuickBooks ID": "QB-2030",
    },
    {
        "Account Number": "2040",
        "Account": "Sales Tax Payable",
        "Type": "liability",
        "Parent Account": "2000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Finance",
        "Default Cost Center": "HQ",
        "Project": "",
        "QuickBooks ID": "QB-2040",
    },
    {
        "Account Number": "2050",
        "Account": "Payroll Tax Payable",
        "Type": "liability",
        "Parent Account": "2000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Finance",
        "Default Cost Center": "HQ",
        "Project": "",
        "QuickBooks ID": "QB-2050",
    },
    {
        "Account Number": "2100",
        "Account": "Notes Payable",
        "Type": "liability",
        "Parent Account": "2000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Finance",
        "Default Cost Center": "HQ",
        "Project": "",
        "QuickBooks ID": "QB-2100",
    },
    {
        "Account Number": "3000",
        "Account": "Equity",
        "Type": "equity",
        "Parent Account": "",
        "Postable": "false",
        "Active": "true",
        "Default Department": "",
        "Default Cost Center": "",
        "Project": "",
        "QuickBooks ID": "",
    },
    {
        "Account Number": "3010",
        "Account": "Owner Capital",
        "Type": "equity",
        "Parent Account": "3000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "",
        "Default Cost Center": "",
        "Project": "",
        "QuickBooks ID": "QB-3010",
    },
    {
        "Account Number": "3020",
        "Account": "Retained Earnings",
        "Type": "equity",
        "Parent Account": "3000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "",
        "Default Cost Center": "",
        "Project": "",
        "QuickBooks ID": "QB-3020",
    },
    {
        "Account Number": "4000",
        "Account": "Revenue",
        "Type": "revenue",
        "Parent Account": "",
        "Postable": "false",
        "Active": "true",
        "Default Department": "",
        "Default Cost Center": "",
        "Project": "",
        "QuickBooks ID": "",
    },
    {
        "Account Number": "4010",
        "Account": "Product Sales",
        "Type": "revenue",
        "Parent Account": "4000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Sales",
        "Default Cost Center": "East",
        "Project": "",
        "QuickBooks ID": "QB-4010",
    },
    {
        "Account Number": "4020",
        "Account": "Service Revenue",
        "Type": "revenue",
        "Parent Account": "4000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Services",
        "Default Cost Center": "HQ",
        "Project": "",
        "QuickBooks ID": "QB-4020",
    },
    {
        "Account Number": "4030",
        "Account": "Consulting Revenue",
        "Type": "revenue",
        "Parent Account": "4000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Services",
        "Default Cost Center": "HQ",
        "Project": "Implementation",
        "QuickBooks ID": "QB-4030",
    },
    {
        "Account Number": "5000",
        "Account": "Cost of Goods Sold",
        "Type": "cost of sales",
        "Parent Account": "",
        "Postable": "false",
        "Active": "true",
        "Default Department": "",
        "Default Cost Center": "",
        "Project": "",
        "QuickBooks ID": "",
    },
    {
        "Account Number": "5010",
        "Account": "Materials COGS",
        "Type": "cost of sales",
        "Parent Account": "5000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Operations",
        "Default Cost Center": "Warehouse",
        "Project": "",
        "QuickBooks ID": "QB-5010",
    },
    {
        "Account Number": "5020",
        "Account": "Direct Labor COGS",
        "Type": "cost of sales",
        "Parent Account": "5000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Operations",
        "Default Cost Center": "Field",
        "Project": "",
        "QuickBooks ID": "QB-5020",
    },
    {
        "Account Number": "6000",
        "Account": "Operating Expenses",
        "Type": "expense",
        "Parent Account": "",
        "Postable": "false",
        "Active": "true",
        "Default Department": "",
        "Default Cost Center": "",
        "Project": "",
        "QuickBooks ID": "",
    },
    {
        "Account Number": "6010",
        "Account": "Salaries and Wages",
        "Type": "expense",
        "Parent Account": "6000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "People",
        "Default Cost Center": "HQ",
        "Project": "",
        "QuickBooks ID": "QB-6010",
    },
    {
        "Account Number": "6020",
        "Account": "Payroll Taxes",
        "Type": "expense",
        "Parent Account": "6000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "People",
        "Default Cost Center": "HQ",
        "Project": "",
        "QuickBooks ID": "QB-6020",
    },
    {
        "Account Number": "6030",
        "Account": "Rent Expense",
        "Type": "expense",
        "Parent Account": "6000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Admin",
        "Default Cost Center": "HQ",
        "Project": "",
        "QuickBooks ID": "QB-6030",
    },
    {
        "Account Number": "6040",
        "Account": "Utilities Expense",
        "Type": "expense",
        "Parent Account": "6000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Admin",
        "Default Cost Center": "HQ",
        "Project": "",
        "QuickBooks ID": "QB-6040",
    },
    {
        "Account Number": "6050",
        "Account": "Software Subscriptions",
        "Type": "expense",
        "Parent Account": "6000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "IT",
        "Default Cost Center": "HQ",
        "Project": "",
        "QuickBooks ID": "QB-6050",
    },
    {
        "Account Number": "6060",
        "Account": "Travel and Meals",
        "Type": "expense",
        "Parent Account": "6000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Sales",
        "Default Cost Center": "East",
        "Project": "",
        "QuickBooks ID": "QB-6060",
    },
    {
        "Account Number": "6070",
        "Account": "Professional Fees",
        "Type": "expense",
        "Parent Account": "6000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Finance",
        "Default Cost Center": "HQ",
        "Project": "",
        "QuickBooks ID": "QB-6070",
    },
    {
        "Account Number": "6080",
        "Account": "Insurance Expense",
        "Type": "expense",
        "Parent Account": "6000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Admin",
        "Default Cost Center": "HQ",
        "Project": "",
        "QuickBooks ID": "QB-6080",
    },
    {
        "Account Number": "6090",
        "Account": "Depreciation Expense",
        "Type": "expense",
        "Parent Account": "6000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Finance",
        "Default Cost Center": "HQ",
        "Project": "",
        "QuickBooks ID": "QB-6090",
    },
    {
        "Account Number": "7000",
        "Account": "Other Income",
        "Type": "other income",
        "Parent Account": "",
        "Postable": "false",
        "Active": "true",
        "Default Department": "",
        "Default Cost Center": "",
        "Project": "",
        "QuickBooks ID": "",
    },
    {
        "Account Number": "7010",
        "Account": "Interest Income",
        "Type": "other income",
        "Parent Account": "7000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Finance",
        "Default Cost Center": "HQ",
        "Project": "",
        "QuickBooks ID": "QB-7010",
    },
    {
        "Account Number": "8000",
        "Account": "Other Expenses",
        "Type": "other expense",
        "Parent Account": "",
        "Postable": "false",
        "Active": "true",
        "Default Department": "",
        "Default Cost Center": "",
        "Project": "",
        "QuickBooks ID": "",
    },
    {
        "Account Number": "8010",
        "Account": "Bank Charges",
        "Type": "other expense",
        "Parent Account": "8000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Finance",
        "Default Cost Center": "HQ",
        "Project": "",
        "QuickBooks ID": "QB-8010",
    },
    {
        "Account Number": "8020",
        "Account": "Interest Expense",
        "Type": "other expense",
        "Parent Account": "8000",
        "Postable": "true",
        "Active": "true",
        "Default Department": "Finance",
        "Default Cost Center": "HQ",
        "Project": "",
        "QuickBooks ID": "QB-8020",
    },
)


def ensure_directories() -> None:
    SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    COA_DIR.mkdir(parents=True, exist_ok=True)


def decimal_string(value: Decimal | str) -> str:
    if isinstance(value, Decimal):
        return format(value.quantize(Decimal("0.01")), "f")
    return str(value)


def write_pdf(path: Path, pages: list[list[str]], title: str) -> None:
    pdf = canvas.Canvas(str(path), pagesize=LETTER)
    _, height = LETTER
    for page_number, lines in enumerate(pages, start=1):
        y_position = height - 54
        pdf.setTitle(title)
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(54, y_position, title)
        y_position -= 28
        pdf.setFont("Helvetica", 10)
        for line in lines:
            if y_position < 54:
                pdf.showPage()
                pdf.setFont("Helvetica", 10)
                y_position = height - 54
            pdf.drawString(54, y_position, line)
            y_position -= 14
        pdf.setFont("Helvetica-Oblique", 9)
        pdf.drawString(54, 32, f"Stress-test fixture page {page_number}")
        pdf.showPage()
    pdf.save()


def create_invoice_pdf(
    path: Path,
    fields: dict[str, object],
    line_items: tuple[dict[str, object], ...],
) -> None:
    lines = [
        "Document Type: Invoice",
        f"Vendor: {fields['vendor_name']}",
        f"Customer: {fields['customer_name']}",
        f"Invoice Number: {fields['invoice_number']}",
        f"Invoice Date: {fields['invoice_date']}",
        f"Due Date: {fields['due_date']}",
        f"Currency: {fields['currency']}",
        f"Total: {decimal_string(fields['total'])}",
        f"Subtotal: {decimal_string(fields['subtotal'])}",
        f"Tax Amount: {decimal_string(fields['tax_amount'])}",
        f"Payment Terms: {fields['payment_terms']}",
        f"Vendor Address: {fields['vendor_address']}",
        f"Vendor Tax ID: {fields['vendor_tax_id']}",
        f"Customer Tax ID: {fields['customer_tax_id']}",
        f"Discount Amount: {decimal_string(fields['discount_amount'])}",
        f"Tax Rate: {fields['tax_rate']}",
        f"Notes: {fields['notes']}",
        "",
        "Line Items",
    ]
    for item in line_items:
        lines.append(
            " | ".join(
                (
                    str(item["description"]),
                    decimal_string(item["quantity"]),
                    decimal_string(item["unit_price"]),
                    decimal_string(item["amount"]),
                    decimal_string(item["tax_amount"]),
                )
            )
        )
    write_pdf(path, [lines], title="Tax Invoice")


def create_payslip_pdf(path: Path) -> None:
    lines = [
        "Document Type: Payslip",
        f"Employee Name: {PAYSLIP_FIELDS['employee_name']}",
        f"Employee ID: {PAYSLIP_FIELDS['employee_id']}",
        f"Employer Name: {PAYSLIP_FIELDS['employer_name']}",
        f"Pay Date: {PAYSLIP_FIELDS['pay_date']}",
        f"Net Pay: {decimal_string(PAYSLIP_FIELDS['net_pay'])}",
        f"Gross Pay: {decimal_string(PAYSLIP_FIELDS['gross_pay'])}",
        f"Basic Salary: {decimal_string(PAYSLIP_FIELDS['basic_salary'])}",
        f"Allowances: {decimal_string(PAYSLIP_FIELDS['allowances'])}",
        f"Deductions: {decimal_string(PAYSLIP_FIELDS['deductions'])}",
        f"PAYE Tax: {decimal_string(PAYSLIP_FIELDS['paye_tax'])}",
        f"Pension Contribution: {decimal_string(PAYSLIP_FIELDS['pension_contribution'])}",
        f"Currency: {PAYSLIP_FIELDS['currency']}",
        "",
        "Deduction Type | Amount",
    ]
    for deduction in PAYSLIP_DEDUCTIONS:
        lines.append(f"{deduction['deduction_type']} | {decimal_string(deduction['amount'])}")
    write_pdf(path, [lines], title="Payslip")


def create_contract_pdf(path: Path) -> None:
    page_one = [
        "Document Type: Contract",
        f"Contract Number: {CONTRACT_FIELDS['contract_number']}",
        f"Contract Date: {CONTRACT_FIELDS['contract_date']}",
        f"Effective Date: {CONTRACT_FIELDS['effective_date']}",
        f"Expiration Date: {CONTRACT_FIELDS['expiration_date']}",
        f"Party A: {CONTRACT_FIELDS['party_a_name']}",
        f"Party B: {CONTRACT_FIELDS['party_b_name']}",
        f"Contract Value: {decimal_string(CONTRACT_FIELDS['contract_value'])}",
        f"Currency: {CONTRACT_FIELDS['currency']}",
        f"Contract Type: {CONTRACT_FIELDS['contract_type']}",
        "",
        "Key Terms",
        f"Terms: {CONTRACT_FIELDS['terms']}",
        f"Renewal Terms: {CONTRACT_FIELDS['renewal_terms']}",
    ]
    page_two = [
        "Master Service Agreement",
        "Additional Conditions",
        f"Termination Terms: {CONTRACT_FIELDS['termination_terms']}",
        "The parties agree to monthly status reporting, documented acceptance testing,",
        "and named approvers for scope changes above USD 5,000.00.",
        "",
        "Signature Block",
        "Party A Authorized Signatory: Alex Morgan, CFO",
        "Party B Authorized Signatory: Priya Shah, Managing Director",
    ]
    write_pdf(path, [page_one, page_two], title="Master Service Agreement")


def create_bank_statement_pdf(path: Path) -> None:
    opening_balance = Decimal("68500.00")
    total_debits = sum((line.debit for line in BANK_TRANSACTION_LINES), Decimal("0.00"))
    total_credits = sum((line.credit for line in BANK_TRANSACTION_LINES), Decimal("0.00"))
    closing_balance = opening_balance + total_credits - total_debits

    lines = [
        "Document Type: Bank Statement",
        "Bank Name: First Citizens Bank",
        "Account Name: Northwind Traders LLC Operating",
        "Account Number: 9876543210",
        f"Statement Start Date: {CLOSE_PERIOD_START.isoformat()}",
        f"Statement End Date: {CLOSE_PERIOD_END.isoformat()}",
        "Currency: USD",
        f"Opening Balance: {decimal_string(opening_balance)}",
        f"Closing Balance: {decimal_string(closing_balance)}",
        f"Credits Total: {decimal_string(total_credits)}",
        f"Debits Total: {decimal_string(total_debits)}",
        "",
        "Date | Description | Reference | Debit | Credit | Balance",
    ]

    running_balance = opening_balance
    for line in BANK_TRANSACTION_LINES:
        running_balance = running_balance + line.credit - line.debit
        lines.append(
            " | ".join(
                (
                    line.date.isoformat(),
                    line.description,
                    line.reference,
                    decimal_string(line.debit),
                    decimal_string(line.credit),
                    decimal_string(running_balance),
                )
            )
        )

    write_pdf(path, [lines], title="Bank Statement")


def create_scanned_bank_statement_pdf(path: Path) -> None:
    opening_balance = Decimal("68500.00")
    total_debits = sum((line.debit for line in BANK_TRANSACTION_LINES), Decimal("0.00"))
    total_credits = sum((line.credit for line in BANK_TRANSACTION_LINES), Decimal("0.00"))
    closing_balance = opening_balance + total_credits - total_debits

    lines = [
        "BANK STATEMENT",
        "Bank Name: First Citizens Bank",
        "Account Name: Northwind Traders LLC Operating",
        "Account Number: 9876543210",
        f"Statement Start Date: {CLOSE_PERIOD_START.isoformat()}",
        f"Statement End Date: {CLOSE_PERIOD_END.isoformat()}",
        "Currency: USD",
        f"Opening Balance: {decimal_string(opening_balance)}",
        f"Closing Balance: {decimal_string(closing_balance)}",
        f"Credits Total: {decimal_string(total_credits)}",
        f"Debits Total: {decimal_string(total_debits)}",
        "",
        "Date | Description | Reference | Debit | Credit | Balance",
    ]

    running_balance = opening_balance
    for line in BANK_TRANSACTION_LINES:
        running_balance = running_balance + line.credit - line.debit
        lines.append(
            " | ".join(
                (
                    line.date.isoformat(),
                    line.description,
                    line.reference,
                    decimal_string(line.debit),
                    decimal_string(line.credit),
                    decimal_string(running_balance),
                )
            )
        )

    image = Image.new("L", (1700, 2200), color=252)
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            size=28,
        )
    except OSError:
        font = ImageFont.load_default()

    y_position = 110
    for line in lines:
        draw.text((95, y_position), line, fill=12, font=font)
        y_position += 58

    # Make the page look scan-like while keeping it suitable for OCR tools.
    image = image.rotate(0.4, expand=False, fillcolor=252)
    image = image.filter(ImageFilter.GaussianBlur(radius=0.45))

    image_buffer = BytesIO()
    image.save(image_buffer, format="PNG")
    image_buffer.seek(0)

    pdf = canvas.Canvas(str(path), pagesize=LETTER)
    pdf.drawImage(ImageReader(image_buffer), 0, 0, width=LETTER[0], height=LETTER[1])
    pdf.showPage()
    pdf.save()


def create_invoice_workbook(
    path: Path,
    *,
    sheet_title: str,
    summary_fields: dict[str, object],
    line_items: tuple[dict[str, object], ...],
) -> None:
    workbook = xlsxwriter.Workbook(str(path))
    header_format = workbook.add_format({"bold": True, "bg_color": "#D9EAF7", "border": 1})
    money_format = workbook.add_format({"num_format": "#,##0.00"})

    summary_sheet = workbook.add_worksheet(sheet_title)
    headers = (
        "Document Type",
        "Vendor",
        "Vendor Address",
        "TIN",
        "Customer",
        "Customer Tax ID",
        "Invoice Number",
        "Invoice Date",
        "Due Date",
        "Currency",
        "Subtotal",
        "Tax Amount",
        "Tax Rate",
        "Discount",
        "Total",
        "Payment Terms",
        "Notes",
    )
    for column, header in enumerate(headers):
        summary_sheet.write(0, column, header, header_format)

    values = (
        "Invoice",
        summary_fields["vendor_name"],
        summary_fields["vendor_address"],
        summary_fields["vendor_tax_id"],
        summary_fields["customer_name"],
        summary_fields["customer_tax_id"],
        summary_fields["invoice_number"],
        summary_fields["invoice_date"],
        summary_fields["due_date"],
        summary_fields["currency"],
        float(Decimal(str(summary_fields["subtotal"]))),
        float(Decimal(str(summary_fields["tax_amount"]))),
        float(Decimal(str(summary_fields["tax_rate"]))),
        float(Decimal(str(summary_fields["discount_amount"]))),
        float(Decimal(str(summary_fields["total"]))),
        summary_fields["payment_terms"],
        summary_fields["notes"],
    )
    for column, value in enumerate(values):
        if isinstance(value, date):
            summary_sheet.write(1, column, value.isoformat())
        elif isinstance(value, float) and column in {10, 11, 12, 13, 14}:
            summary_sheet.write_number(1, column, value, money_format)
        else:
            summary_sheet.write(1, column, value)

    items_sheet = workbook.add_worksheet("Line Items")
    item_headers = ("Description", "Quantity", "Unit Price", "Amount", "Tax Amount")
    for column, header in enumerate(item_headers):
        items_sheet.write(0, column, header, header_format)
    for row_index, item in enumerate(line_items, start=1):
        items_sheet.write(row_index, 0, str(item["description"]))
        items_sheet.write_number(row_index, 1, float(Decimal(str(item["quantity"]))), money_format)
        items_sheet.write_number(
            row_index,
            2,
            float(Decimal(str(item["unit_price"]))),
            money_format,
        )
        items_sheet.write_number(row_index, 3, float(Decimal(str(item["amount"]))), money_format)
        items_sheet.write_number(
            row_index,
            4,
            float(Decimal(str(item["tax_amount"]))),
            money_format,
        )

    for worksheet in (summary_sheet, items_sheet):
        worksheet.set_column(0, 16, 20)
    workbook.close()


def create_payslip_workbook(path: Path) -> None:
    workbook = xlsxwriter.Workbook(str(path))
    header_format = workbook.add_format({"bold": True, "bg_color": "#E6F4EA", "border": 1})
    money_format = workbook.add_format({"num_format": "#,##0.00"})

    summary_sheet = workbook.add_worksheet("Payslip Summary")
    headers = (
        "Document Type",
        "Employee Name",
        "Employee ID",
        "Employer Name",
        "Pay Period Start",
        "Pay Period End",
        "Pay Date",
        "Basic Salary",
        "Allowances",
        "Deductions",
        "Gross Pay",
        "Net Pay",
        "Currency",
        "PAYE Tax",
        "Pension Contribution",
    )
    for column, header in enumerate(headers):
        summary_sheet.write(0, column, header, header_format)

    values = (
        "Payslip",
        PAYSLIP_FIELDS["employee_name"],
        PAYSLIP_FIELDS["employee_id"],
        PAYSLIP_FIELDS["employer_name"],
        PAYSLIP_FIELDS["pay_period_start"],
        PAYSLIP_FIELDS["pay_period_end"],
        PAYSLIP_FIELDS["pay_date"],
        float(PAYSLIP_FIELDS["basic_salary"]),
        float(PAYSLIP_FIELDS["allowances"]),
        float(PAYSLIP_FIELDS["deductions"]),
        float(PAYSLIP_FIELDS["gross_pay"]),
        float(PAYSLIP_FIELDS["net_pay"]),
        PAYSLIP_FIELDS["currency"],
        float(PAYSLIP_FIELDS["paye_tax"]),
        float(PAYSLIP_FIELDS["pension_contribution"]),
    )
    for column, value in enumerate(values):
        if isinstance(value, date):
            summary_sheet.write(1, column, value.isoformat())
        elif isinstance(value, float) and column >= 7 and column != 12:
            summary_sheet.write_number(1, column, value, money_format)
        else:
            summary_sheet.write(1, column, value)

    deduction_sheet = workbook.add_worksheet("Deductions")
    deduction_headers = ("Deduction Type", "Amount")
    for column, header in enumerate(deduction_headers):
        deduction_sheet.write(0, column, header, header_format)
    for row_index, deduction in enumerate(PAYSLIP_DEDUCTIONS, start=1):
        deduction_sheet.write(row_index, 0, str(deduction["deduction_type"]))
        deduction_sheet.write_number(
            row_index,
            1,
            float(Decimal(str(deduction["amount"]))),
            money_format,
        )

    for worksheet in (summary_sheet, deduction_sheet):
        worksheet.set_column(0, 14, 20)
    workbook.close()


def create_contract_workbook(path: Path) -> None:
    workbook = xlsxwriter.Workbook(str(path))
    header_format = workbook.add_format({"bold": True, "bg_color": "#FBE9D5", "border": 1})
    money_format = workbook.add_format({"num_format": "#,##0.00"})

    summary_sheet = workbook.add_worksheet("Contract Summary")
    headers = (
        "Document Type",
        "Contract Number",
        "Contract Date",
        "Effective Date",
        "Expiration Date",
        "Party A Name",
        "Party B Name",
        "Contract Value",
        "Currency",
        "Contract Type",
        "Terms",
        "Renewal Terms",
        "Termination Terms",
    )
    for column, header in enumerate(headers):
        summary_sheet.write(0, column, header, header_format)

    values = (
        "Contract",
        CONTRACT_FIELDS["contract_number"],
        CONTRACT_FIELDS["contract_date"],
        CONTRACT_FIELDS["effective_date"],
        CONTRACT_FIELDS["expiration_date"],
        CONTRACT_FIELDS["party_a_name"],
        CONTRACT_FIELDS["party_b_name"],
        float(CONTRACT_FIELDS["contract_value"]),
        CONTRACT_FIELDS["currency"],
        CONTRACT_FIELDS["contract_type"],
        CONTRACT_FIELDS["terms"],
        CONTRACT_FIELDS["renewal_terms"],
        CONTRACT_FIELDS["termination_terms"],
    )
    for column, value in enumerate(values):
        if isinstance(value, date):
            summary_sheet.write(1, column, value.isoformat())
        elif isinstance(value, float) and column == 7:
            summary_sheet.write_number(1, column, value, money_format)
        else:
            summary_sheet.write(1, column, value)

    clauses_sheet = workbook.add_worksheet("Clauses")
    clauses_sheet.write_row("A1", ("Section", "Clause"), header_format)
    clauses = (
        ("Scope", "Data migration, dashboard setup, and training are included."),
        ("Billing", "Vendor bills monthly in arrears against approved milestones."),
        ("Change Control", "Changes above USD 5,000.00 require written approval."),
        ("Termination", str(CONTRACT_FIELDS["termination_terms"])),
    )
    for row_index, clause in enumerate(clauses, start=1):
        clauses_sheet.write_row(row_index, 0, clause)

    for worksheet in (summary_sheet, clauses_sheet):
        worksheet.set_column(0, 12, 28)
    workbook.close()


def create_bank_statement_workbook(path: Path) -> None:
    workbook = xlsxwriter.Workbook(str(path))
    header_format = workbook.add_format({"bold": True, "bg_color": "#FFF2CC", "border": 1})
    money_format = workbook.add_format({"num_format": "#,##0.00"})

    statement_sheet = workbook.add_worksheet("Bank Statement Summary")
    headers = (
        "Document Type",
        "Bank Name",
        "Account Name",
        "Account Number",
        "Period Start",
        "Period End",
        "Opening Balance",
        "Closing Balance",
        "Credits Total",
        "Debits Total",
        "Currency",
    )
    for column, header in enumerate(headers):
        statement_sheet.write(0, column, header, header_format)

    opening_balance = Decimal("68500.00")
    total_debits = sum((line.debit for line in BANK_TRANSACTION_LINES), Decimal("0.00"))
    total_credits = sum((line.credit for line in BANK_TRANSACTION_LINES), Decimal("0.00"))
    closing_balance = opening_balance + total_credits - total_debits

    values = (
        "Bank Statement",
        "First Citizens Bank",
        "Northwind Traders LLC Operating",
        "9876543210",
        CLOSE_PERIOD_START,
        CLOSE_PERIOD_END,
        float(opening_balance),
        float(closing_balance),
        float(total_credits),
        float(total_debits),
        "USD",
    )
    for column, value in enumerate(values):
        if isinstance(value, date):
            statement_sheet.write(1, column, value.isoformat())
        elif isinstance(value, float) and column in {6, 7, 8, 9}:
            statement_sheet.write_number(1, column, value, money_format)
        else:
            statement_sheet.write(1, column, value)

    transaction_sheet = workbook.add_worksheet("Transactions")
    transaction_headers = ("Date", "Description", "Reference", "Debit", "Credit", "Balance")
    for column, header in enumerate(transaction_headers):
        transaction_sheet.write(0, column, header, header_format)

    running_balance = opening_balance
    for row_index, line in enumerate(BANK_TRANSACTION_LINES, start=1):
        running_balance = running_balance + line.credit - line.debit
        transaction_sheet.write(row_index, 0, line.date.isoformat())
        transaction_sheet.write(row_index, 1, line.description)
        transaction_sheet.write(row_index, 2, line.reference)
        transaction_sheet.write_number(row_index, 3, float(line.debit), money_format)
        transaction_sheet.write_number(row_index, 4, float(line.credit), money_format)
        transaction_sheet.write_number(row_index, 5, float(running_balance), money_format)

    for worksheet in (statement_sheet, transaction_sheet):
        worksheet.set_column(0, 10, 24)
    workbook.close()


def create_coa_workbook(path: Path) -> None:
    workbook = xlsxwriter.Workbook(str(path))
    header_format = workbook.add_format({"bold": True, "bg_color": "#DDEBF7", "border": 1})

    worksheet = workbook.add_worksheet("US SMB COA")
    headers = tuple(COA_ROWS[0].keys())
    for column, header in enumerate(headers):
        worksheet.write(0, column, header, header_format)
    for row_index, row in enumerate(COA_ROWS, start=1):
        for column, header in enumerate(headers):
            worksheet.write(row_index, column, row[header])
    worksheet.set_column(0, len(headers) - 1, 22)
    workbook.close()


def create_grouped_csv(path: Path) -> None:
    rows = (
        {
            "Document Type": "Invoice",
            "Reference": "INV-1048",
            "Amount": "2450.00",
            "Date": "2026-03-08",
            "Notes": "In-period invoice that should match the bank statement line.",
        },
        {
            "Document Type": "Invoice",
            "Reference": "INV-APR-2201",
            "Amount": "1500.00",
            "Date": "2026-04-04",
            "Notes": "Out-of-period invoice to test close-window validation.",
        },
        {
            "Document Type": "Payslip",
            "Reference": "PR-2026-03-JL",
            "Amount": "4165.00",
            "Date": "2026-03-15",
            "Notes": "Payroll item that should match a statement debit.",
        },
        {
            "Document Type": "Contract",
            "Reference": "MSA-2026-017",
            "Amount": "48000.00",
            "Date": "2026-03-01",
            "Notes": "Contract row group for split-candidate testing.",
        },
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tuple(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def create_manifest() -> None:
    manifest = {
        "recommended_close_run_period": {
            "period_start": CLOSE_PERIOD_START.isoformat(),
            "period_end": CLOSE_PERIOD_END.isoformat(),
        },
        "source_documents": [
            {
                "filename": "invoice-acme-office-fitout-2026-03.xlsx",
                "expected_type": "invoice",
                "purpose": "High-coverage invoice extraction fixture.",
                "amount": "2450.00",
                "date": "2026-03-08",
                "should_match_statement_reference": "INV-1048",
            },
            {
                "filename": "invoice-acme-office-fitout-2026-03.pdf",
                "expected_type": "invoice",
                "purpose": "Realistic PDF invoice for upload and review stress.",
            },
            {
                "filename": "invoice-acme-office-fitout-2026-03-duplicate.pdf",
                "expected_type": "invoice",
                "purpose": "Exact byte-for-byte duplicate for duplicate-upload testing.",
            },
            {
                "filename": "invoice-out-of-period-2026-04.xlsx",
                "expected_type": "invoice",
                "purpose": "Outside the March close window to trigger period review.",
                "date": "2026-04-04",
            },
            {
                "filename": "bank-statement-operating-account-2026-03.xlsx",
                "expected_type": "bank_statement",
                "purpose": "Statement summary plus transaction detail for matching.",
            },
            {
                "filename": "bank-statement-operating-account-2026-03.pdf",
                "expected_type": "bank_statement",
                "purpose": (
                    "PDF bank statement with header fields and a delimited transaction table."
                ),
            },
            {
                "filename": "bank-statement-operating-account-2026-03-scanned.pdf",
                "expected_type": "bank_statement",
                "purpose": "Image-only scanned bank statement fixture that requires OCR.",
                "requires_ocr_runtime": True,
            },
            {
                "filename": "payslip-jordan-lee-2026-03.xlsx",
                "expected_type": "payslip",
                "purpose": "High-coverage payslip extraction fixture.",
                "amount": "4165.00",
                "date": "2026-03-15",
                "should_match_statement_reference": "PR-2026-03-JL",
            },
            {
                "filename": "payslip-jordan-lee-2026-03.pdf",
                "expected_type": "payslip",
                "purpose": "Realistic PDF payslip for upload and review stress.",
            },
            {
                "filename": "contract-cedar-ridge-msa-2026.xlsx",
                "expected_type": "contract",
                "purpose": "High-coverage contract extraction fixture.",
            },
            {
                "filename": "contract-cedar-ridge-msa-2026.pdf",
                "expected_type": "contract",
                "purpose": "Realistic multi-page contract for PDF handling stress.",
            },
            {
                "filename": "grouped-source-documents.csv",
                "expected_type": "unknown",
                "purpose": "Grouped CSV that should produce split candidates by document type.",
            },
        ],
        "coa_files": [
            {
                "filename": "sample-us-smb-coa.xlsx",
                "purpose": (
                    "US small-business chart of accounts using human-friendly alias headers."
                ),
                "account_count": len(COA_ROWS),
            }
        ],
    }
    (PACK_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def main() -> None:
    ensure_directories()

    invoice_xlsx = SOURCE_DIR / "invoice-acme-office-fitout-2026-03.xlsx"
    invoice_pdf = SOURCE_DIR / "invoice-acme-office-fitout-2026-03.pdf"
    duplicate_invoice_pdf = SOURCE_DIR / "invoice-acme-office-fitout-2026-03-duplicate.pdf"
    out_of_period_invoice = SOURCE_DIR / "invoice-out-of-period-2026-04.xlsx"
    bank_statement = SOURCE_DIR / "bank-statement-operating-account-2026-03.xlsx"
    bank_statement_pdf = SOURCE_DIR / "bank-statement-operating-account-2026-03.pdf"
    scanned_bank_statement_pdf = SOURCE_DIR / "bank-statement-operating-account-2026-03-scanned.pdf"
    payslip_xlsx = SOURCE_DIR / "payslip-jordan-lee-2026-03.xlsx"
    payslip_pdf = SOURCE_DIR / "payslip-jordan-lee-2026-03.pdf"
    contract_xlsx = SOURCE_DIR / "contract-cedar-ridge-msa-2026.xlsx"
    contract_pdf = SOURCE_DIR / "contract-cedar-ridge-msa-2026.pdf"
    grouped_csv = SOURCE_DIR / "grouped-source-documents.csv"
    coa_xlsx = COA_DIR / "sample-us-smb-coa.xlsx"

    create_invoice_workbook(
        invoice_xlsx,
        sheet_title="Invoice Summary",
        summary_fields=INVOICE_FIELDS,
        line_items=INVOICE_LINE_ITEMS,
    )
    create_invoice_pdf(invoice_pdf, INVOICE_FIELDS, INVOICE_LINE_ITEMS)
    shutil.copyfile(invoice_pdf, duplicate_invoice_pdf)

    create_invoice_workbook(
        out_of_period_invoice,
        sheet_title="Invoice Summary",
        summary_fields=OUT_OF_PERIOD_INVOICE_FIELDS,
        line_items=OUT_OF_PERIOD_INVOICE_LINE_ITEMS,
    )
    create_bank_statement_workbook(bank_statement)
    create_bank_statement_pdf(bank_statement_pdf)
    create_scanned_bank_statement_pdf(scanned_bank_statement_pdf)
    create_payslip_workbook(payslip_xlsx)
    create_payslip_pdf(payslip_pdf)
    create_contract_workbook(contract_xlsx)
    create_contract_pdf(contract_pdf)
    create_grouped_csv(grouped_csv)
    create_coa_workbook(coa_xlsx)
    create_manifest()

    print(f"Generated stress-test fixture pack at {PACK_DIR}")


if __name__ == "__main__":
    main()
