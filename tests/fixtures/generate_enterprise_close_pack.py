from __future__ import annotations

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
PACK_DIR = ROOT / "enterprise-close-pack-ngn"
SOURCE_DIR = PACK_DIR / "source-documents"
INVOICES_DIR = SOURCE_DIR / "invoices"
CONTRACTS_DIR = SOURCE_DIR / "contracts"
PAYSLIPS_DIR = SOURCE_DIR / "payslips"
BANK_STATEMENTS_DIR = SOURCE_DIR / "bank-statements"
COA_DIR = PACK_DIR / "coa"
LEDGER_DIR = PACK_DIR / "ledger"

CLOSE_PERIOD_START = date(2026, 3, 1)
CLOSE_PERIOD_END = date(2026, 3, 31)
COMPANY_NAME = "Apex Meridian Distribution Limited"
COMPANY_TAX_ID = "NG-TIN-12844319-0001"
COMPANY_RC = "RC 1284431"
BASE_CURRENCY = "NGN"


@dataclass(frozen=True, slots=True)
class InvoiceLineItem:
    description: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal


@dataclass(frozen=True, slots=True)
class InvoiceFixture:
    slug: str
    vendor_name: str
    vendor_address: str
    vendor_tax_id: str
    invoice_number: str
    invoice_date: date
    due_date: date
    subtotal: Decimal
    tax_amount: Decimal
    total: Decimal
    payment_terms: str
    notes: str
    represented_in_gl: bool
    line_items: tuple[InvoiceLineItem, ...]
    related_contract_number: str | None = None


@dataclass(frozen=True, slots=True)
class PayslipFixture:
    slug: str
    employee_name: str
    employee_id: str
    department: str
    pay_period_start: date
    pay_period_end: date
    pay_date: date
    basic_salary: Decimal
    allowances: Decimal
    paye_tax: Decimal
    pension_contribution: Decimal
    other_deductions: Decimal

    @property
    def gross_pay(self) -> Decimal:
        return self.basic_salary + self.allowances

    @property
    def deductions(self) -> Decimal:
        return self.paye_tax + self.pension_contribution + self.other_deductions

    @property
    def net_pay(self) -> Decimal:
        return self.gross_pay - self.deductions


@dataclass(frozen=True, slots=True)
class ContractFixture:
    slug: str
    contract_number: str
    contract_date: date
    effective_date: date
    expiration_date: date
    party_a_name: str
    party_b_name: str
    contract_value: Decimal
    contract_type: str
    terms: str
    renewal_terms: str
    termination_terms: str


@dataclass(frozen=True, slots=True)
class BankStatementLine:
    posting_date: date
    description: str
    reference: str
    debit: Decimal
    credit: Decimal


@dataclass(frozen=True, slots=True)
class BankStatementFixture:
    slug: str
    bank_name: str
    account_name: str
    account_number: str
    opening_balance: Decimal
    lines: tuple[BankStatementLine, ...]
    account_code: str
    scanned_pdf: bool = False

    @property
    def total_debits(self) -> Decimal:
        return sum((line.debit for line in self.lines), Decimal("0.00"))

    @property
    def total_credits(self) -> Decimal:
        return sum((line.credit for line in self.lines), Decimal("0.00"))

    @property
    def closing_balance(self) -> Decimal:
        return self.opening_balance + self.total_credits - self.total_debits


@dataclass(frozen=True, slots=True)
class JournalLineFixture:
    account_code: str
    debit: Decimal
    credit: Decimal
    reference: str
    description: str
    external_ref: str
    dimensions: dict[str, str]


@dataclass(frozen=True, slots=True)
class JournalEntryFixture:
    journal_number: str
    posting_date: date
    description: str
    lines: tuple[JournalLineFixture, ...]


COA_DEFINITIONS: tuple[tuple[str, str, str, str, str, str, str, str, str, str], ...] = (
    ("1000", "Assets", "asset", "", "false", "true", "", "", "", ""),
    ("1010", "Operating Bank", "asset", "1000", "true", "true", "Finance", "HQ", "", ""),
    ("1015", "Payroll Bank", "asset", "1000", "true", "true", "Finance", "HQ", "", ""),
    ("1020", "Accounts Receivable", "asset", "1000", "true", "true", "Finance", "HQ", "", ""),
    ("1030", "Inventory", "asset", "1000", "true", "true", "Operations", "Warehouse", "", ""),
    ("1040", "Prepayments", "asset", "1000", "true", "true", "Finance", "HQ", "", ""),
    ("1050", "VAT Recoverable", "asset", "1000", "true", "true", "Finance", "HQ", "", ""),
    ("1060", "Security Deposits", "asset", "1000", "true", "true", "Admin", "HQ", "", ""),
    ("1500", "Property Plant and Equipment", "asset", "1000", "false", "true", "", "", "", ""),
    (
        "1510",
        "Warehouse Equipment",
        "asset",
        "1500",
        "true",
        "true",
        "Operations",
        "Warehouse",
        "Capacity Expansion",
        "",
    ),
    (
        "1520",
        "Computer Equipment",
        "asset",
        "1500",
        "true",
        "true",
        "IT",
        "HQ",
        "ERP Refresh",
        "",
    ),
    ("1530", "Leasehold Improvements", "asset", "1500", "true", "true", "Admin", "HQ", "", ""),
    ("2000", "Liabilities", "liability", "", "false", "true", "", "", "", ""),
    ("2010", "Accounts Payable", "liability", "2000", "true", "true", "Finance", "HQ", "", ""),
    ("2050", "PAYE Payable", "liability", "2000", "true", "true", "Finance", "HQ", "", ""),
    ("2060", "Pension Payable", "liability", "2000", "true", "true", "Finance", "HQ", "", ""),
    (
        "2065",
        "Staff Deductions Payable",
        "liability",
        "2000",
        "true",
        "true",
        "Finance",
        "HQ",
        "",
        "",
    ),
    ("2070", "VAT Payable", "liability", "2000", "true", "true", "Finance", "HQ", "", ""),
    ("2100", "Accrued Expenses", "liability", "2000", "true", "true", "Finance", "HQ", "", ""),
    ("2200", "Bank Loan", "liability", "2000", "true", "true", "Finance", "Treasury", "", ""),
    ("3000", "Equity", "equity", "", "false", "true", "", "", "", ""),
    ("3010", "Share Capital", "equity", "3000", "true", "true", "", "", "", ""),
    ("3020", "Retained Earnings", "equity", "3000", "true", "true", "", "", "", ""),
    ("4000", "Revenue", "revenue", "", "false", "true", "", "", "", ""),
    (
        "4010",
        "Distribution Revenue",
        "revenue",
        "4000",
        "true",
        "true",
        "Sales",
        "National",
        "",
        "",
    ),
    (
        "4020",
        "Installation Revenue",
        "revenue",
        "4000",
        "true",
        "true",
        "Projects",
        "Enterprise",
        "",
        "",
    ),
    ("5000", "Cost of Sales", "cost of sales", "", "false", "true", "", "", "", ""),
    (
        "5010",
        "Cost of Goods Sold",
        "cost of sales",
        "5000",
        "true",
        "true",
        "Operations",
        "Warehouse",
        "",
        "",
    ),
    ("6000", "Operating Expenses", "expense", "", "false", "true", "", "", "", ""),
    ("6010", "Salaries and Wages", "expense", "6000", "true", "true", "People", "HQ", "", ""),
    ("6020", "Payroll Taxes", "expense", "6000", "true", "true", "People", "HQ", "", ""),
    ("6030", "Warehouse Rent", "expense", "6000", "true", "true", "Admin", "HQ", "", ""),
    ("6040", "Diesel and Power", "expense", "6000", "true", "true", "Operations", "Fleet", "", ""),
    (
        "6050",
        "Freight and Logistics",
        "expense",
        "6000",
        "true",
        "true",
        "Operations",
        "North Linehaul",
        "",
        "",
    ),
    (
        "6060",
        "Software and Cloud",
        "expense",
        "6000",
        "true",
        "true",
        "IT",
        "HQ",
        "ERP Refresh",
        "",
    ),
    (
        "6070",
        "Marketing Activation",
        "expense",
        "6000",
        "true",
        "true",
        "Sales",
        "Field",
        "Q1 Push",
        "",
    ),
    ("6080", "Professional Fees", "expense", "6000", "true", "true", "Finance", "HQ", "", ""),
    ("6090", "Connectivity Expense", "expense", "6000", "true", "true", "IT", "HQ", "", ""),
    ("6100", "Insurance Expense", "expense", "6000", "true", "true", "Admin", "HQ", "", ""),
    ("6110", "Security Expense", "expense", "6000", "true", "true", "Operations", "HQ", "", ""),
    ("6120", "Utilities Expense", "expense", "6000", "true", "true", "Operations", "HQ", "", ""),
    (
        "6130",
        "Repairs and Maintenance",
        "expense",
        "6000",
        "true",
        "true",
        "Operations",
        "Warehouse",
        "",
        "",
    ),
    (
        "6140",
        "Staff Welfare and Travel",
        "expense",
        "6000",
        "true",
        "true",
        "People",
        "HQ",
        "",
        "",
    ),
    ("6150", "Training Expense", "expense", "6000", "true", "true", "People", "HQ", "", ""),
    ("7000", "Other Income", "other income", "", "false", "true", "", "", "", ""),
    (
        "7010",
        "Interest Income",
        "other income",
        "7000",
        "true",
        "true",
        "Finance",
        "Treasury",
        "",
        "",
    ),
    ("8000", "Other Expenses", "other expense", "", "false", "true", "", "", "", ""),
    (
        "8010",
        "Bank Charges",
        "other expense",
        "8000",
        "true",
        "true",
        "Finance",
        "Treasury",
        "",
        "",
    ),
    (
        "8020",
        "Interest Expense",
        "other expense",
        "8000",
        "true",
        "true",
        "Finance",
        "Treasury",
        "",
        "",
    ),
)

ACCOUNT_NAME_BY_CODE = {row[0]: row[1] for row in COA_DEFINITIONS}
ACCOUNT_TYPE_BY_CODE = {row[0]: row[2] for row in COA_DEFINITIONS if row[4] == "true"}

INVOICES: tuple[InvoiceFixture, ...] = (
    InvoiceFixture(
        slug="invoice-harbor-warehouse-rent-2026-03",
        vendor_name="Harbor Property Holdings Limited",
        vendor_address="12 Marine Road, Apapa, Lagos",
        vendor_tax_id="NG-TIN-HARBOR-0021",
        invoice_number="HPL-2026-031",
        invoice_date=date(2026, 3, 3),
        due_date=date(2026, 3, 10),
        subtotal=Decimal("14500000.00"),
        tax_amount=Decimal("0.00"),
        total=Decimal("14500000.00"),
        payment_terms="Net 7",
        notes="March warehouse lease for the Lagos distribution hub.",
        represented_in_gl=True,
        line_items=(
            InvoiceLineItem(
                "Warehouse base rent - March",
                Decimal("1"),
                Decimal("12500000.00"),
                Decimal("12500000.00"),
            ),
            InvoiceLineItem(
                "Service charge and facility levy",
                Decimal("1"),
                Decimal("2000000.00"),
                Decimal("2000000.00"),
            ),
        ),
        related_contract_number="CTR-WHL-2026-001",
    ),
    InvoiceFixture(
        slug="invoice-nova-diesel-supply-2026-03",
        vendor_name="Nova Energy Solutions Limited",
        vendor_address="18 Creek Road, Port Harcourt, Rivers",
        vendor_tax_id="NG-TIN-NOVA-4471",
        invoice_number="NES-4471",
        invoice_date=date(2026, 3, 7),
        due_date=date(2026, 3, 14),
        subtotal=Decimal("9860000.00"),
        tax_amount=Decimal("0.00"),
        total=Decimal("9860000.00"),
        payment_terms="Net 7",
        notes="Diesel supply for warehouse generators and regional dispatch fleet.",
        represented_in_gl=True,
        line_items=(
            InvoiceLineItem(
                "Generator diesel - Lagos hub",
                Decimal("3800"),
                Decimal("1450.00"),
                Decimal("5510000.00"),
            ),
            InvoiceLineItem(
                "Dispatch fleet diesel - North corridor",
                Decimal("3000"),
                Decimal("1450.00"),
                Decimal("4350000.00"),
            ),
        ),
    ),
    InvoiceFixture(
        slug="invoice-axis-haulage-2026-03",
        vendor_name="Axis Haulage & Logistics Plc",
        vendor_address="4 Zaria Bypass, Kano, Kano",
        vendor_tax_id="NG-TIN-AXIS-8820",
        invoice_number="AHL-8820",
        invoice_date=date(2026, 3, 9),
        due_date=date(2026, 3, 16),
        subtotal=Decimal("12740000.00"),
        tax_amount=Decimal("0.00"),
        total=Decimal("12740000.00"),
        payment_terms="Net 7",
        notes="Line-haul freight and secondary distribution services for March.",
        represented_in_gl=True,
        line_items=(
            InvoiceLineItem(
                "North line-haul distribution",
                Decimal("1"),
                Decimal("7440000.00"),
                Decimal("7440000.00"),
            ),
            InvoiceLineItem(
                "South-East final-mile distribution",
                Decimal("1"),
                Decimal("5300000.00"),
                Decimal("5300000.00"),
            ),
        ),
        related_contract_number="CTR-LOG-2026-014",
    ),
    InvoiceFixture(
        slug="invoice-signal-security-services-2026-03",
        vendor_name="Signal Guard Services Limited",
        vendor_address="9 Admiralty Way, Lekki Phase 1, Lagos",
        vendor_tax_id="NG-TIN-SIGNAL-1103",
        invoice_number="SGS-1103",
        invoice_date=date(2026, 3, 10),
        due_date=date(2026, 3, 17),
        subtotal=Decimal("5940000.00"),
        tax_amount=Decimal("0.00"),
        total=Decimal("5940000.00"),
        payment_terms="Net 7",
        notes="Armed response and facility access control services for March.",
        represented_in_gl=True,
        line_items=(
            InvoiceLineItem(
                "Static guards - Lagos HQ",
                Decimal("1"),
                Decimal("3140000.00"),
                Decimal("3140000.00"),
            ),
            InvoiceLineItem(
                "Warehouse perimeter response team",
                Decimal("1"),
                Decimal("2800000.00"),
                Decimal("2800000.00"),
            ),
        ),
        related_contract_number="CTR-SEC-2026-007",
    ),
    InvoiceFixture(
        slug="invoice-cloud-erp-subscription-2026-03",
        vendor_name="CloudCore Technology Services Limited",
        vendor_address="15 Bishop Aboyade Cole Street, Victoria Island, Lagos",
        vendor_tax_id="NG-TIN-CTS-2026",
        invoice_number="CTS-2026-03",
        invoice_date=date(2026, 3, 12),
        due_date=date(2026, 3, 19),
        subtotal=Decimal("4280000.00"),
        tax_amount=Decimal("0.00"),
        total=Decimal("4280000.00"),
        payment_terms="Net 7",
        notes="Monthly ERP, CRM, and warehouse management subscription bundle.",
        represented_in_gl=True,
        line_items=(
            InvoiceLineItem(
                "ERP core subscription", Decimal("1"), Decimal("2280000.00"), Decimal("2280000.00")
            ),
            InvoiceLineItem(
                "CRM and WMS modules", Decimal("1"), Decimal("2000000.00"), Decimal("2000000.00")
            ),
        ),
        related_contract_number="CTR-ERP-2026-021",
    ),
    InvoiceFixture(
        slug="invoice-kano-field-marketing-2026-03",
        vendor_name="Kano Field Marketing Associates",
        vendor_address="22 Zoo Road, Kano, Kano",
        vendor_tax_id="NG-TIN-KFM-0118",
        invoice_number="KFM-118",
        invoice_date=date(2026, 3, 14),
        due_date=date(2026, 3, 21),
        subtotal=Decimal("7650000.00"),
        tax_amount=Decimal("0.00"),
        total=Decimal("7650000.00"),
        payment_terms="Net 7",
        notes="Activation booths, promoter fees, and retail visibility rollout in Kano.",
        represented_in_gl=True,
        line_items=(
            InvoiceLineItem(
                "Retail activation manpower",
                Decimal("1"),
                Decimal("4150000.00"),
                Decimal("4150000.00"),
            ),
            InvoiceLineItem(
                "Booth setup and visibility materials",
                Decimal("1"),
                Decimal("3500000.00"),
                Decimal("3500000.00"),
            ),
        ),
    ),
    InvoiceFixture(
        slug="invoice-vertex-forklifts-2026-03",
        vendor_name="Vertex Forklifts Nigeria Limited",
        vendor_address="1 Challenge Road, Ibadan, Oyo",
        vendor_tax_id="NG-TIN-VERTEX-2091",
        invoice_number="VFL-2091",
        invoice_date=date(2026, 3, 17),
        due_date=date(2026, 3, 24),
        subtotal=Decimal("28400000.00"),
        tax_amount=Decimal("0.00"),
        total=Decimal("28400000.00"),
        payment_terms="Net 7",
        notes="Counterbalance forklift and battery package for warehouse expansion.",
        represented_in_gl=True,
        line_items=(
            InvoiceLineItem(
                "3-ton electric forklift",
                Decimal("1"),
                Decimal("24750000.00"),
                Decimal("24750000.00"),
            ),
            InvoiceLineItem(
                "Battery, charger, and operator kit",
                Decimal("1"),
                Decimal("3650000.00"),
                Decimal("3650000.00"),
            ),
        ),
    ),
    InvoiceFixture(
        slug="invoice-datahub-connectivity-2026-03",
        vendor_name="DataHub Connectivity Services Limited",
        vendor_address="10 Aromire Avenue, Ikeja, Lagos",
        vendor_tax_id="NG-TIN-DHC-7712",
        invoice_number="DHC-7712",
        invoice_date=date(2026, 3, 18),
        due_date=date(2026, 3, 25),
        subtotal=Decimal("3950000.00"),
        tax_amount=Decimal("0.00"),
        total=Decimal("3950000.00"),
        payment_terms="Net 7",
        notes="Warehouse MPLS, branch connectivity, and backup internet for March.",
        represented_in_gl=True,
        line_items=(
            InvoiceLineItem(
                "HQ MPLS and branch circuits",
                Decimal("1"),
                Decimal("2550000.00"),
                Decimal("2550000.00"),
            ),
            InvoiceLineItem(
                "Backup internet and failover devices",
                Decimal("1"),
                Decimal("1400000.00"),
                Decimal("1400000.00"),
            ),
        ),
    ),
    InvoiceFixture(
        slug="invoice-oceanic-audit-retainer-2026-03",
        vendor_name="Oceanic Assurance Partners",
        vendor_address="6 Akin Adesola Street, Victoria Island, Lagos",
        vendor_tax_id="NG-TIN-OAP-9004",
        invoice_number="OAP-9004",
        invoice_date=date(2026, 3, 21),
        due_date=date(2026, 3, 28),
        subtotal=Decimal("8500000.00"),
        tax_amount=Decimal("0.00"),
        total=Decimal("8500000.00"),
        payment_terms="Net 7",
        notes="Quarter-end audit retainer intentionally left out of the imported GL.",
        represented_in_gl=False,
        line_items=(
            InvoiceLineItem(
                "Quarter-end audit fieldwork retainer",
                Decimal("1"),
                Decimal("8500000.00"),
                Decimal("8500000.00"),
            ),
        ),
    ),
    InvoiceFixture(
        slug="invoice-north-gate-customs-clearing-2026-03",
        vendor_name="North Gate Customs Clearing Limited",
        vendor_address="8 Wharf Road, Tin Can Island, Lagos",
        vendor_tax_id="NG-TIN-NGC-0319",
        invoice_number="NGC-0319",
        invoice_date=date(2026, 3, 23),
        due_date=date(2026, 3, 30),
        subtotal=Decimal("6980000.00"),
        tax_amount=Decimal("0.00"),
        total=Decimal("6980000.00"),
        payment_terms="Net 7",
        notes="Clearing and demurrage charges intentionally left unbooked in the imported GL.",
        represented_in_gl=False,
        line_items=(
            InvoiceLineItem(
                "Port clearing fees", Decimal("1"), Decimal("4630000.00"), Decimal("4630000.00")
            ),
            InvoiceLineItem(
                "Demurrage and documentation",
                Decimal("1"),
                Decimal("2350000.00"),
                Decimal("2350000.00"),
            ),
        ),
    ),
    InvoiceFixture(
        slug="invoice-april-generator-overhaul-2026-04",
        vendor_name="Prime Power Works Limited",
        vendor_address="11 Sapara Williams Close, Victoria Island, Lagos",
        vendor_tax_id="NG-TIN-PPW-4406",
        invoice_number="PPW-4406",
        invoice_date=date(2026, 4, 4),
        due_date=date(2026, 4, 11),
        subtotal=Decimal("6800000.00"),
        tax_amount=Decimal("0.00"),
        total=Decimal("6800000.00"),
        payment_terms="Net 7",
        notes="April overhaul invoice for out-of-period validation testing.",
        represented_in_gl=False,
        line_items=(
            InvoiceLineItem(
                "Generator overhaul and alternator replacement",
                Decimal("1"),
                Decimal("6800000.00"),
                Decimal("6800000.00"),
            ),
        ),
    ),
)

PAYSLIPS: tuple[PayslipFixture, ...] = (
    PayslipFixture(
        "payslip-adaobi-nwosu-2026-03",
        "Adaobi Nwosu",
        "EMP-1001",
        "Finance",
        CLOSE_PERIOD_START,
        CLOSE_PERIOD_END,
        date(2026, 3, 25),
        Decimal("2100000.00"),
        Decimal("500000.00"),
        Decimal("330000.00"),
        Decimal("110000.00"),
        Decimal("60000.00"),
    ),
    PayslipFixture(
        "payslip-tunde-afolayan-2026-03",
        "Tunde Afolayan",
        "EMP-1002",
        "Operations",
        CLOSE_PERIOD_START,
        CLOSE_PERIOD_END,
        date(2026, 3, 25),
        Decimal("1650000.00"),
        Decimal("380000.00"),
        Decimal("250000.00"),
        Decimal("80000.00"),
        Decimal("50000.00"),
    ),
    PayslipFixture(
        "payslip-mariam-bello-2026-03",
        "Mariam Bello",
        "EMP-1003",
        "Sales",
        CLOSE_PERIOD_START,
        CLOSE_PERIOD_END,
        date(2026, 3, 25),
        Decimal("1480000.00"),
        Decimal("340000.00"),
        Decimal("220000.00"),
        Decimal("80000.00"),
        Decimal("40000.00"),
    ),
    PayslipFixture(
        "payslip-emeka-okoro-2026-03",
        "Emeka Okoro",
        "EMP-1004",
        "Warehouse",
        CLOSE_PERIOD_START,
        CLOSE_PERIOD_END,
        date(2026, 3, 25),
        Decimal("1210000.00"),
        Decimal("280000.00"),
        Decimal("170000.00"),
        Decimal("70000.00"),
        Decimal("0.00"),
    ),
    PayslipFixture(
        "payslip-ifeoma-eze-2026-03",
        "Ifeoma Eze",
        "EMP-1005",
        "IT",
        CLOSE_PERIOD_START,
        CLOSE_PERIOD_END,
        date(2026, 3, 25),
        Decimal("1060000.00"),
        Decimal("240000.00"),
        Decimal("150000.00"),
        Decimal("60000.00"),
        Decimal("30000.00"),
    ),
    PayslipFixture(
        "payslip-hassan-garba-2026-03",
        "Hassan Garba",
        "EMP-1006",
        "Logistics",
        CLOSE_PERIOD_START,
        CLOSE_PERIOD_END,
        date(2026, 3, 25),
        Decimal("950000.00"),
        Decimal("210000.00"),
        Decimal("120000.00"),
        Decimal("60000.00"),
        Decimal("30000.00"),
    ),
    PayslipFixture(
        "payslip-chisom-umeh-2026-03",
        "Chisom Umeh",
        "EMP-1007",
        "Projects",
        CLOSE_PERIOD_START,
        CLOSE_PERIOD_END,
        date(2026, 3, 25),
        Decimal("1220000.00"),
        Decimal("270000.00"),
        Decimal("150000.00"),
        Decimal("80000.00"),
        Decimal("40000.00"),
    ),
    PayslipFixture(
        "payslip-binta-yusuf-2026-03",
        "Binta Yusuf",
        "EMP-1008",
        "People",
        CLOSE_PERIOD_START,
        CLOSE_PERIOD_END,
        date(2026, 3, 25),
        Decimal("1300000.00"),
        Decimal("200000.00"),
        Decimal("230000.00"),
        Decimal("10000.00"),
        Decimal("0.00"),
    ),
)

CONTRACTS: tuple[ContractFixture, ...] = (
    ContractFixture(
        slug="contract-lagos-warehouse-lease-2026",
        contract_number="CTR-WHL-2026-001",
        contract_date=date(2026, 1, 10),
        effective_date=date(2026, 1, 15),
        expiration_date=date(2028, 1, 14),
        party_a_name=COMPANY_NAME,
        party_b_name="Harbor Property Holdings Limited",
        contract_value=Decimal("174000000.00"),
        contract_type="Warehouse Lease",
        terms="Exclusive lease of the Apapa warehouse complex with monthly rent billed in arrears.",
        renewal_terms="Renews for 24 months unless either party gives 60 days notice.",
        termination_terms="Termination for uncured material breach after 30 days written notice.",
    ),
    ContractFixture(
        slug="contract-axis-haulage-msa-2026",
        contract_number="CTR-LOG-2026-014",
        contract_date=date(2026, 2, 6),
        effective_date=date(2026, 2, 10),
        expiration_date=date(2027, 2, 9),
        party_a_name=COMPANY_NAME,
        party_b_name="Axis Haulage & Logistics Plc",
        contract_value=Decimal("153000000.00"),
        contract_type="National Line-Haul Services",
        terms=(
            "Dedicated line-haul and secondary distribution support across "
            "North and South-East corridors."
        ),
        renewal_terms="Renews annually unless terminated with 45 days written notice.",
        termination_terms=(
            "Immediate termination for repeated delivery SLA failure or cargo misconduct."
        ),
    ),
    ContractFixture(
        slug="contract-signal-security-services-2026",
        contract_number="CTR-SEC-2026-007",
        contract_date=date(2026, 1, 22),
        effective_date=date(2026, 2, 1),
        expiration_date=date(2027, 1, 31),
        party_a_name=COMPANY_NAME,
        party_b_name="Signal Guard Services Limited",
        contract_value=Decimal("71280000.00"),
        contract_type="Security and Response Services",
        terms=(
            "Static guard coverage, access control, and mobile response for "
            "headquarters and warehouse sites."
        ),
        renewal_terms="Renews for 12 months unless either party provides 30 days notice.",
        termination_terms=(
            "Termination for service failure not cured within 14 days of escalation notice."
        ),
    ),
    ContractFixture(
        slug="contract-cloudcore-erp-subscription-2026",
        contract_number="CTR-ERP-2026-021",
        contract_date=date(2026, 2, 15),
        effective_date=date(2026, 3, 1),
        expiration_date=date(2027, 2, 28),
        party_a_name=COMPANY_NAME,
        party_b_name="CloudCore Technology Services Limited",
        contract_value=Decimal("51360000.00"),
        contract_type="ERP and Warehouse Systems Subscription",
        terms="ERP core, CRM, and warehouse management subscriptions billed monthly in arrears.",
        renewal_terms=(
            "Renews automatically for successive 12-month periods unless 30 days notice is given."
        ),
        termination_terms=(
            "Either party may terminate for uncured material breach after 15 days written notice."
        ),
    ),
)

OPERATING_STATEMENT = BankStatementFixture(
    slug="bank-statement-operating-account-2026-03",
    bank_name="Citadel Commercial Bank Plc",
    account_name=f"{COMPANY_NAME} Operating Account",
    account_number="3000149827",
    opening_balance=Decimal("182400000.00"),
    account_code="1010",
    scanned_pdf=True,
    lines=(
        BankStatementLine(
            date(2026, 3, 2),
            "CUSTOMER COLLECTION OMNI RETAIL GROUP",
            "CUST-OMNI-001",
            Decimal("0.00"),
            Decimal("48500000.00"),
        ),
        BankStatementLine(
            date(2026, 3, 4),
            "HARBOR PROPERTY HOLDINGS HPL-2026-031",
            "HPL-2026-031",
            Decimal("14500000.00"),
            Decimal("0.00"),
        ),
        BankStatementLine(
            date(2026, 3, 6),
            "CUSTOMER COLLECTION NORTHERN HUB",
            "CUST-NORTH-002",
            Decimal("0.00"),
            Decimal("32000000.00"),
        ),
        BankStatementLine(
            date(2026, 3, 8),
            "NOVA ENERGY SOLUTIONS NES-4471",
            "NES-4471",
            Decimal("9860000.00"),
            Decimal("0.00"),
        ),
        BankStatementLine(
            date(2026, 3, 10),
            "AXIS HAULAGE AHL-8820",
            "AHL-8820",
            Decimal("12740000.00"),
            Decimal("0.00"),
        ),
        BankStatementLine(
            date(2026, 3, 11),
            "SIGNAL GUARD SERVICES SGS-1103",
            "SGS-1103",
            Decimal("5940000.00"),
            Decimal("0.00"),
        ),
        BankStatementLine(
            date(2026, 3, 13),
            "CLOUDCORE SUBSCRIPTION CTS-2026-03",
            "CTS-2026-03",
            Decimal("4280000.00"),
            Decimal("0.00"),
        ),
        BankStatementLine(
            date(2026, 3, 15),
            "CUSTOMER COLLECTION SOUTH EAST",
            "CUST-SE-003",
            Decimal("0.00"),
            Decimal("54120000.00"),
        ),
        BankStatementLine(
            date(2026, 3, 16),
            "KANO FIELD MARKETING KFM-118",
            "KFM-118",
            Decimal("7650000.00"),
            Decimal("0.00"),
        ),
        BankStatementLine(
            date(2026, 3, 18),
            "VERTEX FORKLIFTS VFL-2091",
            "VFL-2091",
            Decimal("28400000.00"),
            Decimal("0.00"),
        ),
        BankStatementLine(
            date(2026, 3, 19),
            "DATAHUB CONNECTIVITY DHC-7712",
            "DHC-7712",
            Decimal("3950000.00"),
            Decimal("0.00"),
        ),
        BankStatementLine(
            date(2026, 3, 20),
            "TRANSFER TO PAYROLL ACCOUNT",
            "TRF-PAYROLL-0320",
            Decimal("13390000.00"),
            Decimal("0.00"),
        ),
        BankStatementLine(
            date(2026, 3, 22),
            "INSURANCE PREMIUM MARCH",
            "INS-2026-03",
            Decimal("2450000.00"),
            Decimal("0.00"),
        ),
        BankStatementLine(
            date(2026, 3, 24),
            "INTEREST CREDIT MARCH",
            "INT-MAR-2026",
            Decimal("0.00"),
            Decimal("120000.00"),
        ),
        BankStatementLine(
            date(2026, 3, 25),
            "UTILITY PAYMENT HQ AND WAREHOUSE",
            "UTIL-3308",
            Decimal("1280000.00"),
            Decimal("0.00"),
        ),
        BankStatementLine(
            date(2026, 3, 27),
            "REPAIRS AND MAINTENANCE WAREHOUSE",
            "RPR-2041",
            Decimal("2760000.00"),
            Decimal("0.00"),
        ),
        BankStatementLine(
            date(2026, 3, 29),
            "STAFF WELFARE AND TRAVEL",
            "WLF-1189",
            Decimal("1340000.00"),
            Decimal("0.00"),
        ),
        BankStatementLine(
            date(2026, 3, 30),
            "MONTHLY BANK CHARGES",
            "FEE-MAR-2026",
            Decimal("85000.00"),
            Decimal("0.00"),
        ),
        BankStatementLine(
            date(2026, 3, 31),
            "LOAN INTEREST PAYMENT",
            "INTEXP-0331",
            Decimal("640000.00"),
            Decimal("0.00"),
        ),
    ),
)

PAYROLL_STATEMENT = BankStatementFixture(
    slug="bank-statement-payroll-account-2026-03",
    bank_name="Citadel Commercial Bank Plc",
    account_name=f"{COMPANY_NAME} Payroll Account",
    account_number="3000149839",
    opening_balance=Decimal("250000.00"),
    account_code="1015",
    lines=(
        BankStatementLine(
            date(2026, 3, 20),
            "FUNDING FROM OPERATING ACCOUNT",
            "TRF-PAYROLL-0320",
            Decimal("0.00"),
            Decimal("13390000.00"),
        ),
        BankStatementLine(
            date(2026, 3, 25),
            "SALARY BATCH MARCH 2026",
            "SAL-BATCH-0325",
            Decimal("10970000.00"),
            Decimal("0.00"),
        ),
        BankStatementLine(
            date(2026, 3, 26),
            "PAYE REMITTANCE FIRS",
            "PAYE-MAR-2026",
            Decimal("1620000.00"),
            Decimal("0.00"),
        ),
        BankStatementLine(
            date(2026, 3, 27),
            "PENSION REMITTANCE PFM",
            "PEN-MAR-2026",
            Decimal("550000.00"),
            Decimal("0.00"),
        ),
        BankStatementLine(
            date(2026, 3, 28),
            "STAFF DEDUCTION REMITTANCE",
            "DED-MAR-2026",
            Decimal("250000.00"),
            Decimal("0.00"),
        ),
    ),
)

BANK_STATEMENTS: tuple[BankStatementFixture, ...] = (OPERATING_STATEMENT, PAYROLL_STATEMENT)

OPENING_BALANCES: dict[str, Decimal] = {
    "1010": Decimal("182400000.00"),
    "1015": Decimal("250000.00"),
    "1020": Decimal("54000000.00"),
    "1030": Decimal("37500000.00"),
    "1040": Decimal("6800000.00"),
    "1050": Decimal("4400000.00"),
    "1060": Decimal("8000000.00"),
    "1510": Decimal("44200000.00"),
    "1520": Decimal("18600000.00"),
    "2010": Decimal("-19700000.00"),
    "2050": Decimal("-1620000.00"),
    "2060": Decimal("-550000.00"),
    "2065": Decimal("-250000.00"),
    "2100": Decimal("-6400000.00"),
    "2200": Decimal("-42000000.00"),
    "3010": Decimal("-120000000.00"),
    "3020": Decimal("-165630000.00"),
}


def ensure_directories() -> None:
    if PACK_DIR.exists():
        shutil.rmtree(PACK_DIR)
    INVOICES_DIR.mkdir(parents=True, exist_ok=True)
    CONTRACTS_DIR.mkdir(parents=True, exist_ok=True)
    PAYSLIPS_DIR.mkdir(parents=True, exist_ok=True)
    BANK_STATEMENTS_DIR.mkdir(parents=True, exist_ok=True)
    COA_DIR.mkdir(parents=True, exist_ok=True)
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)


def decimal_string(value: Decimal | str) -> str:
    if isinstance(value, Decimal):
        return format(value.quantize(Decimal("0.01")), "f")
    return str(value)


def write_pdf(path: Path, pages: list[list[str]], title: str) -> None:
    pdf = canvas.Canvas(str(path), pagesize=LETTER)
    _, height = LETTER
    for page_number, lines in enumerate(pages, start=1):
        pdf.setTitle(title)
        y_position = height - 54
        pdf.setFont("Helvetica-Bold", 15)
        pdf.drawString(54, y_position, title)
        y_position -= 24
        pdf.setFont("Helvetica", 9)
        for line in lines:
            if y_position < 54:
                pdf.showPage()
                pdf.setFont("Helvetica", 9)
                y_position = height - 54
            pdf.drawString(54, y_position, line)
            y_position -= 13
        pdf.setFont("Helvetica-Oblique", 8)
        pdf.drawString(54, 28, f"Enterprise close-run fixture page {page_number}")
        pdf.showPage()
    pdf.save()


def create_invoice_pdf(path: Path, invoice: InvoiceFixture) -> None:
    lines = [
        "Document Type: Invoice",
        f"Vendor: {invoice.vendor_name}",
        f"Vendor Address: {invoice.vendor_address}",
        f"Vendor Tax ID: {invoice.vendor_tax_id}",
        f"Customer: {COMPANY_NAME}",
        f"Customer Tax ID: {COMPANY_TAX_ID}",
        f"Customer RC: {COMPANY_RC}",
        f"Invoice Number: {invoice.invoice_number}",
        f"Invoice Date: {invoice.invoice_date.isoformat()}",
        f"Due Date: {invoice.due_date.isoformat()}",
        f"Currency: {BASE_CURRENCY}",
        f"Subtotal: {decimal_string(invoice.subtotal)}",
        f"Tax Amount: {decimal_string(invoice.tax_amount)}",
        f"Total: {decimal_string(invoice.total)}",
        f"Payment Terms: {invoice.payment_terms}",
        f"Represented In Imported GL: {'Yes' if invoice.represented_in_gl else 'No'}",
        f"Related Contract: {invoice.related_contract_number or 'None'}",
        f"Notes: {invoice.notes}",
        "",
        "Line Items",
    ]
    for item in invoice.line_items:
        lines.append(
            " | ".join(
                (
                    item.description,
                    decimal_string(item.quantity),
                    decimal_string(item.unit_price),
                    decimal_string(item.amount),
                )
            )
        )
    write_pdf(path, [lines], "Vendor Tax Invoice")


def create_invoice_workbook(path: Path, invoice: InvoiceFixture) -> None:
    workbook = xlsxwriter.Workbook(str(path))
    header_format = workbook.add_format({"bold": True, "bg_color": "#D9EAF7", "border": 1})
    money_format = workbook.add_format({"num_format": "#,##0.00"})

    summary_sheet = workbook.add_worksheet("Invoice Summary")
    headers = (
        "Document Type",
        "Vendor",
        "Vendor Address",
        "Vendor Tax ID",
        "Customer",
        "Customer Tax ID",
        "Customer RC",
        "Invoice Number",
        "Invoice Date",
        "Due Date",
        "Currency",
        "Subtotal",
        "Tax Amount",
        "Total",
        "Payment Terms",
        "Represented In Imported GL",
        "Related Contract",
        "Notes",
    )
    for column, header in enumerate(headers):
        summary_sheet.write(0, column, header, header_format)

    values = (
        "Invoice",
        invoice.vendor_name,
        invoice.vendor_address,
        invoice.vendor_tax_id,
        COMPANY_NAME,
        COMPANY_TAX_ID,
        COMPANY_RC,
        invoice.invoice_number,
        invoice.invoice_date.isoformat(),
        invoice.due_date.isoformat(),
        BASE_CURRENCY,
        float(invoice.subtotal),
        float(invoice.tax_amount),
        float(invoice.total),
        invoice.payment_terms,
        "true" if invoice.represented_in_gl else "false",
        invoice.related_contract_number or "",
        invoice.notes,
    )
    for column, value in enumerate(values):
        if column in {11, 12, 13}:
            summary_sheet.write_number(1, column, float(value), money_format)
        else:
            summary_sheet.write(1, column, value)

    items_sheet = workbook.add_worksheet("Line Items")
    items_sheet.write_row(0, 0, ("Description", "Quantity", "Unit Price", "Amount"), header_format)
    for row_index, item in enumerate(invoice.line_items, start=1):
        items_sheet.write(row_index, 0, item.description)
        items_sheet.write_number(row_index, 1, float(item.quantity), money_format)
        items_sheet.write_number(row_index, 2, float(item.unit_price), money_format)
        items_sheet.write_number(row_index, 3, float(item.amount), money_format)

    summary_sheet.set_column(0, 17, 20)
    items_sheet.set_column(0, 3, 26)
    workbook.close()


def create_payslip_pdf(path: Path, payslip: PayslipFixture) -> None:
    lines = [
        "Document Type: Payslip",
        f"Employer Name: {COMPANY_NAME}",
        f"Employee Name: {payslip.employee_name}",
        f"Employee ID: {payslip.employee_id}",
        f"Department: {payslip.department}",
        f"Pay Period Start: {payslip.pay_period_start.isoformat()}",
        f"Pay Period End: {payslip.pay_period_end.isoformat()}",
        f"Pay Date: {payslip.pay_date.isoformat()}",
        f"Currency: {BASE_CURRENCY}",
        f"Basic Salary: {decimal_string(payslip.basic_salary)}",
        f"Allowances: {decimal_string(payslip.allowances)}",
        f"Gross Pay: {decimal_string(payslip.gross_pay)}",
        f"PAYE Tax: {decimal_string(payslip.paye_tax)}",
        f"Pension Contribution: {decimal_string(payslip.pension_contribution)}",
        f"Other Deductions: {decimal_string(payslip.other_deductions)}",
        f"Deductions: {decimal_string(payslip.deductions)}",
        f"Net Pay: {decimal_string(payslip.net_pay)}",
    ]
    write_pdf(path, [lines], "Payroll Payslip")


def create_payslip_workbook(path: Path, payslip: PayslipFixture) -> None:
    workbook = xlsxwriter.Workbook(str(path))
    header_format = workbook.add_format({"bold": True, "bg_color": "#E6F4EA", "border": 1})
    money_format = workbook.add_format({"num_format": "#,##0.00"})

    summary_sheet = workbook.add_worksheet("Payslip Summary")
    headers = (
        "Document Type",
        "Employer Name",
        "Employee Name",
        "Employee ID",
        "Department",
        "Pay Period Start",
        "Pay Period End",
        "Pay Date",
        "Basic Salary",
        "Allowances",
        "Gross Pay",
        "PAYE Tax",
        "Pension Contribution",
        "Other Deductions",
        "Deductions",
        "Net Pay",
        "Currency",
    )
    for column, header in enumerate(headers):
        summary_sheet.write(0, column, header, header_format)

    values = (
        "Payslip",
        COMPANY_NAME,
        payslip.employee_name,
        payslip.employee_id,
        payslip.department,
        payslip.pay_period_start.isoformat(),
        payslip.pay_period_end.isoformat(),
        payslip.pay_date.isoformat(),
        float(payslip.basic_salary),
        float(payslip.allowances),
        float(payslip.gross_pay),
        float(payslip.paye_tax),
        float(payslip.pension_contribution),
        float(payslip.other_deductions),
        float(payslip.deductions),
        float(payslip.net_pay),
        BASE_CURRENCY,
    )
    for column, value in enumerate(values):
        if column in {8, 9, 10, 11, 12, 13, 14, 15}:
            summary_sheet.write_number(1, column, float(value), money_format)
        else:
            summary_sheet.write(1, column, value)

    deduction_sheet = workbook.add_worksheet("Deductions")
    deduction_sheet.write_row(0, 0, ("Deduction Type", "Amount"), header_format)
    rows = (
        ("PAYE Tax", payslip.paye_tax),
        ("Pension Contribution", payslip.pension_contribution),
        ("Other Deductions", payslip.other_deductions),
    )
    for row_index, (label, amount) in enumerate(rows, start=1):
        deduction_sheet.write(row_index, 0, label)
        deduction_sheet.write_number(row_index, 1, float(amount), money_format)

    summary_sheet.set_column(0, 16, 18)
    deduction_sheet.set_column(0, 1, 22)
    workbook.close()


def create_contract_pdf(path: Path, contract: ContractFixture) -> None:
    page_one = [
        "Document Type: Contract",
        f"Contract Number: {contract.contract_number}",
        f"Contract Date: {contract.contract_date.isoformat()}",
        f"Effective Date: {contract.effective_date.isoformat()}",
        f"Expiration Date: {contract.expiration_date.isoformat()}",
        f"Party A: {contract.party_a_name}",
        f"Party B: {contract.party_b_name}",
        f"Contract Type: {contract.contract_type}",
        f"Contract Value: {decimal_string(contract.contract_value)}",
        f"Currency: {BASE_CURRENCY}",
        "",
        "Key Terms",
        contract.terms,
    ]
    page_two = [
        "Renewal Terms",
        contract.renewal_terms,
        "",
        "Termination Terms",
        contract.termination_terms,
        "",
        "Authorized Signatories",
        "Party A: Kemi Adeyemi, Chief Financial Officer",
        "Party B: Duly Authorized Representative",
    ]
    write_pdf(path, [page_one, page_two], "Commercial Agreement")


def create_contract_workbook(path: Path, contract: ContractFixture) -> None:
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
        contract.contract_number,
        contract.contract_date.isoformat(),
        contract.effective_date.isoformat(),
        contract.expiration_date.isoformat(),
        contract.party_a_name,
        contract.party_b_name,
        float(contract.contract_value),
        BASE_CURRENCY,
        contract.contract_type,
        contract.terms,
        contract.renewal_terms,
        contract.termination_terms,
    )
    for column, value in enumerate(values):
        if column == 7:
            summary_sheet.write_number(1, column, float(value), money_format)
        else:
            summary_sheet.write(1, column, value)

    clauses_sheet = workbook.add_worksheet("Clauses")
    clauses_sheet.write_row(0, 0, ("Section", "Clause"), header_format)
    clauses = (
        ("Scope", contract.terms),
        ("Renewal", contract.renewal_terms),
        ("Termination", contract.termination_terms),
        (
            "Service Credit",
            "Material service failures are escalated through named contract owners.",
        ),
    )
    for row_index, clause in enumerate(clauses, start=1):
        clauses_sheet.write_row(row_index, 0, clause)

    summary_sheet.set_column(0, 12, 24)
    clauses_sheet.set_column(0, 1, 38)
    workbook.close()


def create_bank_statement_pdf(path: Path, statement: BankStatementFixture) -> None:
    lines = [
        "Document Type: Bank Statement",
        f"Bank Name: {statement.bank_name}",
        f"Account Name: {statement.account_name}",
        f"Account Number: {statement.account_number}",
        f"Statement Start Date: {CLOSE_PERIOD_START.isoformat()}",
        f"Statement End Date: {CLOSE_PERIOD_END.isoformat()}",
        f"Currency: {BASE_CURRENCY}",
        f"Opening Balance: {decimal_string(statement.opening_balance)}",
        f"Closing Balance: {decimal_string(statement.closing_balance)}",
        f"Credits Total: {decimal_string(statement.total_credits)}",
        f"Debits Total: {decimal_string(statement.total_debits)}",
        "",
        "Date | Description | Reference | Debit | Credit | Balance",
    ]
    running_balance = statement.opening_balance
    for line in statement.lines:
        running_balance = running_balance + line.credit - line.debit
        lines.append(
            " | ".join(
                (
                    line.posting_date.isoformat(),
                    line.description,
                    line.reference,
                    decimal_string(line.debit),
                    decimal_string(line.credit),
                    decimal_string(running_balance),
                )
            )
        )
    write_pdf(path, [lines], "Bank Statement")


def create_scanned_bank_statement_pdf(path: Path, statement: BankStatementFixture) -> None:
    lines = [
        "BANK STATEMENT",
        f"Bank Name: {statement.bank_name}",
        f"Account Name: {statement.account_name}",
        f"Account Number: {statement.account_number}",
        f"Statement Start Date: {CLOSE_PERIOD_START.isoformat()}",
        f"Statement End Date: {CLOSE_PERIOD_END.isoformat()}",
        f"Opening Balance: {decimal_string(statement.opening_balance)}",
        f"Closing Balance: {decimal_string(statement.closing_balance)}",
        f"Credits Total: {decimal_string(statement.total_credits)}",
        f"Debits Total: {decimal_string(statement.total_debits)}",
        "",
        "Date | Description | Reference | Debit | Credit | Balance",
    ]
    running_balance = statement.opening_balance
    for line in statement.lines:
        running_balance = running_balance + line.credit - line.debit
        lines.append(
            " | ".join(
                (
                    line.posting_date.isoformat(),
                    line.description,
                    line.reference,
                    decimal_string(line.debit),
                    decimal_string(line.credit),
                    decimal_string(running_balance),
                )
            )
        )

    image = Image.new("L", (1700, 2400), color=252)
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            size=26,
        )
    except OSError:
        font = ImageFont.load_default()

    y_position = 110
    for line in lines:
        draw.text((90, y_position), line, fill=12, font=font)
        y_position += 56

    image = image.rotate(0.5, expand=False, fillcolor=252)
    image = image.filter(ImageFilter.GaussianBlur(radius=0.45))

    image_buffer = BytesIO()
    image.save(image_buffer, format="PNG")
    image_buffer.seek(0)

    pdf = canvas.Canvas(str(path), pagesize=LETTER)
    pdf.drawImage(ImageReader(image_buffer), 0, 0, width=LETTER[0], height=LETTER[1])
    pdf.showPage()
    pdf.save()


def create_bank_statement_workbook(path: Path, statement: BankStatementFixture) -> None:
    workbook = xlsxwriter.Workbook(str(path))
    header_format = workbook.add_format({"bold": True, "bg_color": "#FFF2CC", "border": 1})
    money_format = workbook.add_format({"num_format": "#,##0.00"})

    summary_sheet = workbook.add_worksheet("Bank Statement Summary")
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
    summary_sheet.write_row(0, 0, headers, header_format)

    values = (
        "Bank Statement",
        statement.bank_name,
        statement.account_name,
        statement.account_number,
        CLOSE_PERIOD_START.isoformat(),
        CLOSE_PERIOD_END.isoformat(),
        float(statement.opening_balance),
        float(statement.closing_balance),
        float(statement.total_credits),
        float(statement.total_debits),
        BASE_CURRENCY,
    )
    for column, value in enumerate(values):
        if column in {6, 7, 8, 9}:
            summary_sheet.write_number(1, column, float(value), money_format)
        else:
            summary_sheet.write(1, column, value)

    transaction_sheet = workbook.add_worksheet("Transactions")
    transaction_sheet.write_row(
        0,
        0,
        ("Date", "Description", "Reference", "Debit", "Credit", "Balance"),
        header_format,
    )
    running_balance = statement.opening_balance
    for row_index, line in enumerate(statement.lines, start=1):
        running_balance = running_balance + line.credit - line.debit
        transaction_sheet.write(row_index, 0, line.posting_date.isoformat())
        transaction_sheet.write(row_index, 1, line.description)
        transaction_sheet.write(row_index, 2, line.reference)
        transaction_sheet.write_number(row_index, 3, float(line.debit), money_format)
        transaction_sheet.write_number(row_index, 4, float(line.credit), money_format)
        transaction_sheet.write_number(row_index, 5, float(running_balance), money_format)

    summary_sheet.set_column(0, 10, 24)
    transaction_sheet.set_column(0, 5, 26)
    workbook.close()


def create_coa_workbook(path: Path) -> None:
    workbook = xlsxwriter.Workbook(str(path))
    header_format = workbook.add_format({"bold": True, "bg_color": "#DDEBF7", "border": 1})
    worksheet = workbook.add_worksheet("NG Enterprise COA")
    headers = (
        "Account Number",
        "Account",
        "Type",
        "Parent Account",
        "Postable",
        "Active",
        "Default Department",
        "Default Cost Center",
        "Project",
        "QuickBooks ID",
    )
    worksheet.write_row(0, 0, headers, header_format)
    for row_index, row in enumerate(COA_DEFINITIONS, start=1):
        worksheet.write_row(row_index, 0, row)
    worksheet.set_column(0, len(headers) - 1, 22)
    workbook.close()


def create_coa_pdf(path: Path) -> None:
    lines = [
        "Document Type: Chart of Accounts",
        f"Entity: {COMPANY_NAME}",
        f"Currency: {BASE_CURRENCY}",
        "",
        "Account Number | Account | Type | Parent | Postable | Active",
    ]
    for row in COA_DEFINITIONS:
        lines.append(" | ".join(row[:6]))
    write_pdf(path, [lines], "Chart of Accounts")


def journal_entry(
    *,
    journal_number: str,
    posting_date: date,
    reference: str,
    description: str,
    debit_account_code: str,
    credit_account_code: str,
    amount: Decimal,
    debit_dimensions: dict[str, str] | None = None,
    credit_dimensions: dict[str, str] | None = None,
    external_ref: str | None = None,
) -> JournalEntryFixture:
    resolved_external_ref = external_ref or reference
    return JournalEntryFixture(
        journal_number=journal_number,
        posting_date=posting_date,
        description=description,
        lines=(
            JournalLineFixture(
                account_code=debit_account_code,
                debit=amount,
                credit=Decimal("0.00"),
                reference=reference,
                description=description,
                external_ref=resolved_external_ref,
                dimensions=debit_dimensions or {},
            ),
            JournalLineFixture(
                account_code=credit_account_code,
                debit=Decimal("0.00"),
                credit=amount,
                reference=reference,
                description=description,
                external_ref=resolved_external_ref,
                dimensions=credit_dimensions or {},
            ),
        ),
    )


def build_journal_entries() -> tuple[JournalEntryFixture, ...]:
    return (
        journal_entry(
            journal_number="GL-2026-03001",
            posting_date=date(2026, 3, 2),
            reference="CUST-OMNI-001",
            description="Collection from Omni Retail Group",
            debit_account_code="1010",
            credit_account_code="4010",
            amount=Decimal("48500000.00"),
            debit_dimensions={"department": "Finance", "cost_centre": "Treasury"},
            credit_dimensions={"department": "Sales", "cost_centre": "National"},
        ),
        journal_entry(
            journal_number="GL-2026-03002",
            posting_date=date(2026, 3, 4),
            reference="HPL-2026-031",
            description="Warehouse rent payment for March",
            debit_account_code="6030",
            credit_account_code="1010",
            amount=Decimal("14500000.00"),
            debit_dimensions={"department": "Admin", "cost_centre": "HQ"},
            credit_dimensions={"department": "Finance", "cost_centre": "HQ"},
        ),
        journal_entry(
            journal_number="GL-2026-03003",
            posting_date=date(2026, 3, 6),
            reference="CUST-NORTH-002",
            description="Collection from Northern Hub customers",
            debit_account_code="1010",
            credit_account_code="4010",
            amount=Decimal("32000000.00"),
            debit_dimensions={"department": "Finance", "cost_centre": "Treasury"},
            credit_dimensions={"department": "Sales", "cost_centre": "North"},
        ),
        journal_entry(
            journal_number="GL-2026-03004",
            posting_date=date(2026, 3, 8),
            reference="NES-4471",
            description="Diesel procurement for operations",
            debit_account_code="6040",
            credit_account_code="1010",
            amount=Decimal("9860000.00"),
            debit_dimensions={"department": "Operations", "cost_centre": "Fleet"},
            credit_dimensions={"department": "Finance", "cost_centre": "HQ"},
        ),
        journal_entry(
            journal_number="GL-2026-03005",
            posting_date=date(2026, 3, 10),
            reference="AHL-8820",
            description="Freight and logistics payment",
            debit_account_code="6050",
            credit_account_code="1010",
            amount=Decimal("12740000.00"),
            debit_dimensions={"department": "Operations", "cost_centre": "North Linehaul"},
            credit_dimensions={"department": "Finance", "cost_centre": "HQ"},
        ),
        journal_entry(
            journal_number="GL-2026-03006",
            posting_date=date(2026, 3, 11),
            reference="SGS-1103",
            description="Security services payment",
            debit_account_code="6110",
            credit_account_code="1010",
            amount=Decimal("5940000.00"),
            debit_dimensions={"department": "Operations", "cost_centre": "HQ"},
            credit_dimensions={"department": "Finance", "cost_centre": "HQ"},
        ),
        journal_entry(
            journal_number="GL-2026-03007",
            posting_date=date(2026, 3, 13),
            reference="CTS-2026-03",
            description="ERP and cloud subscription payment",
            debit_account_code="6060",
            credit_account_code="1010",
            amount=Decimal("4280000.00"),
            debit_dimensions={"department": "IT", "cost_centre": "HQ", "project": "ERP Refresh"},
            credit_dimensions={"department": "Finance", "cost_centre": "HQ"},
        ),
        journal_entry(
            journal_number="GL-2026-03008",
            posting_date=date(2026, 3, 15),
            reference="CUST-SE-003",
            description="Collection from South-East enterprise customers",
            debit_account_code="1010",
            credit_account_code="4020",
            amount=Decimal("54120000.00"),
            debit_dimensions={"department": "Finance", "cost_centre": "Treasury"},
            credit_dimensions={"department": "Projects", "cost_centre": "Enterprise"},
        ),
        journal_entry(
            journal_number="GL-2026-03009",
            posting_date=date(2026, 3, 16),
            reference="KFM-118",
            description="Field marketing activation payment",
            debit_account_code="6070",
            credit_account_code="1010",
            amount=Decimal("7650000.00"),
            debit_dimensions={"department": "Sales", "cost_centre": "Field", "project": "Q1 Push"},
            credit_dimensions={"department": "Finance", "cost_centre": "HQ"},
        ),
        journal_entry(
            journal_number="GL-2026-03010",
            posting_date=date(2026, 3, 18),
            reference="VFL-2091",
            description="Warehouse forklift capital purchase",
            debit_account_code="1510",
            credit_account_code="1010",
            amount=Decimal("28400000.00"),
            debit_dimensions={
                "department": "Operations",
                "cost_centre": "Warehouse",
                "project": "Capacity Expansion",
            },
            credit_dimensions={"department": "Finance", "cost_centre": "HQ"},
        ),
        journal_entry(
            journal_number="GL-2026-03011",
            posting_date=date(2026, 3, 19),
            reference="DHC-7712",
            description="Connectivity and branch circuits payment",
            debit_account_code="6090",
            credit_account_code="1010",
            amount=Decimal("3950000.00"),
            debit_dimensions={"department": "IT", "cost_centre": "HQ"},
            credit_dimensions={"department": "Finance", "cost_centre": "HQ"},
        ),
        journal_entry(
            journal_number="GL-2026-03012",
            posting_date=date(2026, 3, 20),
            reference="TRF-PAYROLL-0320",
            description="Funding transfer to payroll bank account",
            debit_account_code="1015",
            credit_account_code="1010",
            amount=Decimal("13390000.00"),
            debit_dimensions={"department": "Finance", "cost_centre": "Treasury"},
            credit_dimensions={"department": "Finance", "cost_centre": "Treasury"},
        ),
        journal_entry(
            journal_number="GL-2026-03013",
            posting_date=date(2026, 3, 22),
            reference="INS-2026-03",
            description="Insurance premium payment",
            debit_account_code="6100",
            credit_account_code="1010",
            amount=Decimal("2450000.00"),
            debit_dimensions={"department": "Admin", "cost_centre": "HQ"},
            credit_dimensions={"department": "Finance", "cost_centre": "HQ"},
        ),
        journal_entry(
            journal_number="GL-2026-03014",
            posting_date=date(2026, 3, 24),
            reference="INT-MAR-2026",
            description="Interest income on treasury balances",
            debit_account_code="1010",
            credit_account_code="7010",
            amount=Decimal("120000.00"),
            debit_dimensions={"department": "Finance", "cost_centre": "Treasury"},
            credit_dimensions={"department": "Finance", "cost_centre": "Treasury"},
        ),
        journal_entry(
            journal_number="GL-2026-03015",
            posting_date=date(2026, 3, 25),
            reference="UTIL-3308",
            description="Utilities payment",
            debit_account_code="6120",
            credit_account_code="1010",
            amount=Decimal("1280000.00"),
            debit_dimensions={"department": "Operations", "cost_centre": "HQ"},
            credit_dimensions={"department": "Finance", "cost_centre": "HQ"},
        ),
        journal_entry(
            journal_number="GL-2026-03016",
            posting_date=date(2026, 3, 27),
            reference="RPR-2041",
            description="Repairs and maintenance payment",
            debit_account_code="6130",
            credit_account_code="1010",
            amount=Decimal("2760000.00"),
            debit_dimensions={"department": "Operations", "cost_centre": "Warehouse"},
            credit_dimensions={"department": "Finance", "cost_centre": "HQ"},
        ),
        journal_entry(
            journal_number="GL-2026-03017",
            posting_date=date(2026, 3, 29),
            reference="WLF-1189",
            description="Staff welfare and travel payment",
            debit_account_code="6140",
            credit_account_code="1010",
            amount=Decimal("1340000.00"),
            debit_dimensions={"department": "People", "cost_centre": "HQ"},
            credit_dimensions={"department": "Finance", "cost_centre": "HQ"},
        ),
        journal_entry(
            journal_number="GL-2026-03018",
            posting_date=date(2026, 3, 30),
            reference="FEE-MAR-2026",
            description="Monthly bank charges",
            debit_account_code="8010",
            credit_account_code="1010",
            amount=Decimal("85000.00"),
            debit_dimensions={"department": "Finance", "cost_centre": "Treasury"},
            credit_dimensions={"department": "Finance", "cost_centre": "Treasury"},
        ),
        journal_entry(
            journal_number="GL-2026-03019",
            posting_date=date(2026, 3, 31),
            reference="INTEXP-0331",
            description="Bank loan interest payment",
            debit_account_code="8020",
            credit_account_code="1010",
            amount=Decimal("640000.00"),
            debit_dimensions={"department": "Finance", "cost_centre": "Treasury"},
            credit_dimensions={"department": "Finance", "cost_centre": "Treasury"},
        ),
        journal_entry(
            journal_number="GL-2026-03020",
            posting_date=date(2026, 3, 25),
            reference="SAL-BATCH-0325",
            description="Salary batch payment for March payroll",
            debit_account_code="6010",
            credit_account_code="1015",
            amount=Decimal("10970000.00"),
            debit_dimensions={"department": "People", "cost_centre": "HQ"},
            credit_dimensions={"department": "Finance", "cost_centre": "Treasury"},
        ),
        journal_entry(
            journal_number="GL-2026-03021",
            posting_date=date(2026, 3, 26),
            reference="PAYE-MAR-2026",
            description="PAYE remittance payment",
            debit_account_code="2050",
            credit_account_code="1015",
            amount=Decimal("1620000.00"),
            debit_dimensions={"department": "Finance", "cost_centre": "HQ"},
            credit_dimensions={"department": "Finance", "cost_centre": "Treasury"},
        ),
        journal_entry(
            journal_number="GL-2026-03022",
            posting_date=date(2026, 3, 27),
            reference="PEN-MAR-2026",
            description="Pension remittance payment",
            debit_account_code="2060",
            credit_account_code="1015",
            amount=Decimal("550000.00"),
            debit_dimensions={"department": "Finance", "cost_centre": "HQ"},
            credit_dimensions={"department": "Finance", "cost_centre": "Treasury"},
        ),
        journal_entry(
            journal_number="GL-2026-03023",
            posting_date=date(2026, 3, 28),
            reference="DED-MAR-2026",
            description="Staff deduction remittance payment",
            debit_account_code="2065",
            credit_account_code="1015",
            amount=Decimal("250000.00"),
            debit_dimensions={"department": "Finance", "cost_centre": "HQ"},
            credit_dimensions={"department": "Finance", "cost_centre": "Treasury"},
        ),
    )


JOURNAL_ENTRIES = build_journal_entries()


def ledger_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for entry in JOURNAL_ENTRIES:
        for line in entry.lines:
            rows.append(
                {
                    "Posting Date": entry.posting_date.isoformat(),
                    "Journal Number": entry.journal_number,
                    "Account Code": line.account_code,
                    "Account Name": ACCOUNT_NAME_BY_CODE[line.account_code],
                    "Reference": line.reference,
                    "Description": line.description,
                    "Debit Amount": float(line.debit),
                    "Credit Amount": float(line.credit),
                    "Department": line.dimensions.get("department", ""),
                    "Cost Centre": line.dimensions.get("cost_centre", ""),
                    "Project": line.dimensions.get("project", ""),
                    "External Reference": line.external_ref,
                }
            )
    return rows


def trial_balance_rows() -> list[dict[str, object]]:
    balances = dict(OPENING_BALANCES)
    for entry in JOURNAL_ENTRIES:
        for line in entry.lines:
            signed_amount = line.debit - line.credit
            balances[line.account_code] = (
                balances.get(line.account_code, Decimal("0.00")) + signed_amount
            )

    rows: list[dict[str, object]] = []
    for account_code, account_name in ACCOUNT_NAME_BY_CODE.items():
        if account_code not in ACCOUNT_TYPE_BY_CODE:
            continue
        amount = balances.get(account_code, Decimal("0.00"))
        debit_balance = amount if amount > 0 else Decimal("0.00")
        credit_balance = abs(amount) if amount < 0 else Decimal("0.00")
        rows.append(
            {
                "Account Code": account_code,
                "Account Name": account_name,
                "Account Type": ACCOUNT_TYPE_BY_CODE[account_code],
                "Debit Balance": float(debit_balance),
                "Credit Balance": float(credit_balance),
                "Active": "true",
            }
        )
    return rows


def create_general_ledger_workbook(path: Path) -> None:
    workbook = xlsxwriter.Workbook(str(path))
    header_format = workbook.add_format({"bold": True, "bg_color": "#E2EFDA", "border": 1})
    money_format = workbook.add_format({"num_format": "#,##0.00"})
    worksheet = workbook.add_worksheet("General Ledger")
    rows = ledger_rows()
    headers = tuple(rows[0].keys())
    worksheet.write_row(0, 0, headers, header_format)
    for row_index, row in enumerate(rows, start=1):
        for column, header in enumerate(headers):
            value = row[header]
            if header in {"Debit Amount", "Credit Amount"}:
                worksheet.write_number(row_index, column, float(value), money_format)
            else:
                worksheet.write(row_index, column, value)
    worksheet.set_column(0, len(headers) - 1, 20)
    workbook.close()


def create_general_ledger_pdf(path: Path) -> None:
    lines = [
        "Document Type: General Ledger Import",
        f"Entity: {COMPANY_NAME}",
        f"Period Start: {CLOSE_PERIOD_START.isoformat()}",
        f"Period End: {CLOSE_PERIOD_END.isoformat()}",
        "",
        "Posting Date | Journal Number | Account Code | Reference | Debit | Credit | Description",
    ]
    for row in ledger_rows():
        lines.append(
            " | ".join(
                (
                    str(row["Posting Date"]),
                    str(row["Journal Number"]),
                    str(row["Account Code"]),
                    str(row["Reference"]),
                    decimal_string(Decimal(str(row["Debit Amount"]))),
                    decimal_string(Decimal(str(row["Credit Amount"]))),
                    str(row["Description"]),
                )
            )
        )
    write_pdf(path, [lines], "General Ledger Import")


def create_trial_balance_workbook(path: Path) -> None:
    workbook = xlsxwriter.Workbook(str(path))
    header_format = workbook.add_format({"bold": True, "bg_color": "#F4CCCC", "border": 1})
    money_format = workbook.add_format({"num_format": "#,##0.00"})
    worksheet = workbook.add_worksheet("Trial Balance")
    rows = trial_balance_rows()
    headers = tuple(rows[0].keys())
    worksheet.write_row(0, 0, headers, header_format)
    for row_index, row in enumerate(rows, start=1):
        for column, header in enumerate(headers):
            value = row[header]
            if header in {"Debit Balance", "Credit Balance"}:
                worksheet.write_number(row_index, column, float(value), money_format)
            else:
                worksheet.write(row_index, column, value)
    worksheet.set_column(0, len(headers) - 1, 20)
    workbook.close()


def create_trial_balance_pdf(path: Path) -> None:
    lines = [
        "Document Type: Trial Balance Import",
        f"Entity: {COMPANY_NAME}",
        f"As Of: {CLOSE_PERIOD_END.isoformat()}",
        "",
        "Account Code | Account Name | Account Type | Debit Balance | Credit Balance",
    ]
    for row in trial_balance_rows():
        lines.append(
            " | ".join(
                (
                    str(row["Account Code"]),
                    str(row["Account Name"]),
                    str(row["Account Type"]),
                    decimal_string(Decimal(str(row["Debit Balance"]))),
                    decimal_string(Decimal(str(row["Credit Balance"]))),
                )
            )
        )
    write_pdf(path, [lines], "Trial Balance Import")


def source_document_records() -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    duplicate_path = "source-documents/invoices/invoice-kano-field-marketing-2026-03-duplicate.pdf"

    for invoice in INVOICES:
        base_record = {
            "slug": invoice.slug,
            "document_type": "invoice",
            "amount": decimal_string(invoice.total),
            "date": invoice.invoice_date.isoformat(),
            "represented_in_gl": invoice.represented_in_gl,
            "related_contract_number": invoice.related_contract_number,
            "pdf_path": f"source-documents/invoices/{invoice.slug}.pdf",
            "xlsx_path": f"source-documents/invoices/{invoice.slug}.xlsx",
        }
        records.append(base_record)
        if invoice.slug == "invoice-kano-field-marketing-2026-03":
            records.append(
                {
                    "slug": f"{invoice.slug}-duplicate",
                    "document_type": "invoice",
                    "amount": decimal_string(invoice.total),
                    "date": invoice.invoice_date.isoformat(),
                    "represented_in_gl": invoice.represented_in_gl,
                    "duplicate_of": invoice.slug,
                    "pdf_path": duplicate_path,
                }
            )

    for payslip in PAYSLIPS:
        records.append(
            {
                "slug": payslip.slug,
                "document_type": "payslip",
                "amount": decimal_string(payslip.net_pay),
                "date": payslip.pay_date.isoformat(),
                "represented_in_gl": False,
                "pdf_path": f"source-documents/payslips/{payslip.slug}.pdf",
                "xlsx_path": f"source-documents/payslips/{payslip.slug}.xlsx",
            }
        )

    for contract in CONTRACTS:
        records.append(
            {
                "slug": contract.slug,
                "document_type": "contract",
                "amount": decimal_string(contract.contract_value),
                "date": contract.effective_date.isoformat(),
                "represented_in_gl": False,
                "pdf_path": f"source-documents/contracts/{contract.slug}.pdf",
                "xlsx_path": f"source-documents/contracts/{contract.slug}.xlsx",
            }
        )

    for statement in BANK_STATEMENTS:
        records.append(
            {
                "slug": statement.slug,
                "document_type": "bank_statement",
                "account_code": statement.account_code,
                "statement_start_date": CLOSE_PERIOD_START.isoformat(),
                "statement_end_date": CLOSE_PERIOD_END.isoformat(),
                "pdf_path": f"source-documents/bank-statements/{statement.slug}.pdf",
                "xlsx_path": f"source-documents/bank-statements/{statement.slug}.xlsx",
            }
        )
        if statement.scanned_pdf:
            records.append(
                {
                    "slug": f"{statement.slug}-scanned",
                    "document_type": "bank_statement",
                    "account_code": statement.account_code,
                    "requires_ocr_runtime": True,
                    "pdf_path": f"source-documents/bank-statements/{statement.slug}-scanned.pdf",
                }
            )
    return records


def create_manifest() -> None:
    represented_invoice_total = sum(
        (
            invoice.total
            for invoice in INVOICES
            if invoice.represented_in_gl and invoice.invoice_date <= CLOSE_PERIOD_END
        ),
        Decimal("0.00"),
    )
    unrepresented_invoice_total = sum(
        (
            invoice.total
            for invoice in INVOICES
            if not invoice.represented_in_gl and invoice.invoice_date <= CLOSE_PERIOD_END
        ),
        Decimal("0.00"),
    )
    payroll_net_total = sum((payslip.net_pay for payslip in PAYSLIPS), Decimal("0.00"))
    payroll_gross_total = sum((payslip.gross_pay for payslip in PAYSLIPS), Decimal("0.00"))
    in_period_invoice_total = sum(
        (invoice.total for invoice in INVOICES if invoice.invoice_date <= CLOSE_PERIOD_END),
        Decimal("0.00"),
    )
    manifest = {
        "company": {
            "name": COMPANY_NAME,
            "tax_id": COMPANY_TAX_ID,
            "registration_number": COMPANY_RC,
            "base_currency": BASE_CURRENCY,
        },
        "recommended_close_run_period": {
            "period_start": CLOSE_PERIOD_START.isoformat(),
            "period_end": CLOSE_PERIOD_END.isoformat(),
        },
        "scenario_summary": {
            "in_period_invoice_total": decimal_string(in_period_invoice_total),
            "represented_invoice_total": decimal_string(represented_invoice_total),
            "unrepresented_invoice_total": decimal_string(unrepresented_invoice_total),
            "payroll_net_total": decimal_string(payroll_net_total),
            "payroll_gross_total": decimal_string(payroll_gross_total),
            "operating_statement_closing_balance": decimal_string(
                OPERATING_STATEMENT.closing_balance
            ),
            "payroll_statement_closing_balance": decimal_string(PAYROLL_STATEMENT.closing_balance),
            "general_ledger_row_count": len(ledger_rows()),
            "trial_balance_row_count": len(trial_balance_rows()),
            "document_count": len(source_document_records()),
        },
        "source_documents": source_document_records(),
        "coa_files": [
            {
                "xlsx_path": "coa/apex-meridian-enterprise-coa.xlsx",
                "pdf_path": "coa/apex-meridian-enterprise-coa.pdf",
                "account_count": len(COA_DEFINITIONS),
            }
        ],
        "ledger_files": [
            {
                "xlsx_path": "ledger/apex-meridian-general-ledger-2026-03.xlsx",
                "pdf_path": "ledger/apex-meridian-general-ledger-2026-03.pdf",
                "row_count": len(ledger_rows()),
            },
            {
                "xlsx_path": "ledger/apex-meridian-trial-balance-2026-03.xlsx",
                "pdf_path": "ledger/apex-meridian-trial-balance-2026-03.pdf",
                "row_count": len(trial_balance_rows()),
            },
        ],
        "intended_edge_cases": [
            "duplicate_invoice_pdf",
            "out_of_period_invoice",
            "scanned_bank_statement_pdf",
            "source_documents_not_represented_in_imported_gl",
        ],
    }
    (PACK_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def create_readme() -> None:
    in_period_invoice_total = sum(
        (invoice.total for invoice in INVOICES if invoice.invoice_date <= CLOSE_PERIOD_END),
        Decimal("0.00"),
    )
    represented_invoice_total = sum(
        (
            invoice.total
            for invoice in INVOICES
            if invoice.represented_in_gl and invoice.invoice_date <= CLOSE_PERIOD_END
        ),
        Decimal("0.00"),
    )
    unrepresented_invoice_total = sum(
        (
            invoice.total
            for invoice in INVOICES
            if not invoice.represented_in_gl and invoice.invoice_date <= CLOSE_PERIOD_END
        ),
        Decimal("0.00"),
    )
    payroll_net_total = sum((payslip.net_pay for payslip in PAYSLIPS), Decimal("0.00"))
    text = f"""# Enterprise Close Pack (NGN)

This fixture pack models a March 2026 close run for **{COMPANY_NAME}**.

## What is included

- 11 invoice scenarios in both PDF and XLSX
- 8 payslips in both PDF and XLSX
- 4 contracts in both PDF and XLSX
- 2 bank statements in both PDF and XLSX
- 1 scanned OCR-style operating bank statement PDF
- 1 enterprise COA in XLSX plus PDF reference copy
- 1 March 2026 general ledger import in XLSX plus PDF reference copy
- 1 March 2026 trial balance import in XLSX plus PDF reference copy

## Recommended period

- Period start: {CLOSE_PERIOD_START.isoformat()}
- Period end: {CLOSE_PERIOD_END.isoformat()}

## Useful workflow scenarios

1. Upload only source documents to test parsing, review, duplicate detection, and period checks.
2. Upload COA + GL + TB + source documents to test imported-general-ledger mode.
3. Use the scanned operating statement PDF to test OCR handling.
4. Verify duplicate-post suppression on represented invoices and recommendation
   generation on the unrepresented invoices.

## Expected accounting facts

- In-period invoice total: {decimal_string(in_period_invoice_total)}
- Represented invoice total: {decimal_string(represented_invoice_total)}
- Unrepresented invoice total: {decimal_string(unrepresented_invoice_total)}
- Payroll net pay total: {decimal_string(payroll_net_total)}
- Operating statement closing balance: {decimal_string(OPERATING_STATEMENT.closing_balance)}
- Payroll statement closing balance: {decimal_string(PAYROLL_STATEMENT.closing_balance)}

## Deliberate edge cases

- `invoice-kano-field-marketing-2026-03-duplicate.pdf` is an exact duplicate.
- `invoice-april-generator-overhaul-2026-04.*` is outside the March close window.
- `bank-statement-operating-account-2026-03-scanned.pdf` is image-only.
- `invoice-oceanic-audit-retainer-2026-03.*` and
  `invoice-north-gate-customs-clearing-2026-03.*` are intentionally not
  represented in the imported GL.

## Test docs

- `FULL_PLATFORM_TEST_SUITE.md` walks through the full platform test.
- `CHAT_AGENT_PLATFORM_TEST_PROMPTS.md` contains ready-to-paste prompts for the chat agent.
- `MANUAL_UI_QA_CHECKLIST.md` is a strict click-by-click checklist for manual QA.
"""
    (PACK_DIR / "README.md").write_text(text, encoding="utf-8")


def create_full_platform_test_suite() -> None:
    represented_invoices = "\n".join(
        f"- `{invoice.slug}`"
        for invoice in INVOICES
        if invoice.represented_in_gl and invoice.invoice_date <= CLOSE_PERIOD_END
    )
    unrepresented_invoices = "\n".join(
        f"- `{invoice.slug}`"
        for invoice in INVOICES
        if not invoice.represented_in_gl and invoice.invoice_date <= CLOSE_PERIOD_END
    )
    content = f"""# Full Platform Test Suite

This guide is the operator playbook for a full acceptance test using the
enterprise pack in this folder.

## Scope

This pack is designed to exercise:

- entity and close-run setup
- COA import
- imported general-ledger mode
- trial-balance binding
- source-document upload, parsing, classification, and review
- duplicate detection and out-of-period handling
- deterministic recommendation and duplicate-post suppression behavior
- bank reconciliation with imported ledger-side data
- reporting and export generation
- OCR behavior on a scanned bank statement

## Recommended close run

- Entity / workspace name: `{COMPANY_NAME}`
- Base currency: `{BASE_CURRENCY}`
- Period start: `{CLOSE_PERIOD_START.isoformat()}`
- Period end: `{CLOSE_PERIOD_END.isoformat()}`

## Pack contents

- Source documents root: `source-documents/`
- COA import files: `coa/`
- Ledger imports: `ledger/`
- Machine-readable inventory: `manifest.json`

## High-value scenarios in this pack

1. Imported-books close run:
   Upload COA + GL + TB + source documents and test the full imported-general-ledger path.
2. Source-document review:
   Validate duplicate handling, out-of-period review, OCR routing, and document approvals.
3. Processing controls:
   Confirm represented invoices are suppressed from duplicate posting while
   eligible docs still generate recommendations.
4. Reconciliation:
   Use both bank statements with the imported GL to test true bank reconciliation.
5. Reporting:
   Generate deterministic statements and then test optional narrative commentary.

## Upload order for the cleanest test

1. Upload `coa/apex-meridian-enterprise-coa.xlsx`
2. Upload `ledger/apex-meridian-general-ledger-2026-03.xlsx`
3. Upload `ledger/apex-meridian-trial-balance-2026-03.xlsx`
4. Upload all files under `source-documents/`

This order should place the close run into imported-ledger mode early, which is
the strongest end-to-end path for this pack.

## Expected baseline facts

- Source document count: `27`
- In-period invoice total: `102,800,000.00`
- Represented-in-GL invoice total: `87,320,000.00`
- Unrepresented invoice total: `15,480,000.00`
- Payroll net total: `10,970,000.00`
- GL line count: `46`
- Trial balance row count: `40`

## Collection-phase test

### What to upload

- All invoice PDFs and XLSX files under `source-documents/invoices/`
- All payslip PDFs and XLSX files under `source-documents/payslips/`
- All contract PDFs and XLSX files under `source-documents/contracts/`
- Both digital bank statements and the scanned operating statement

### What should happen

- Invoices should classify as `invoice`
- Payslips should classify as `payslip`
- Contracts should classify as `contract`
- Bank statements should classify as `bank_statement`
- The scanned operating statement should follow the OCR path
- The duplicate PDF should be flagged as a duplicate candidate
- The April invoice should be flagged as outside the March close period

### Deliberate exception documents

- Duplicate:
  `source-documents/invoices/invoice-kano-field-marketing-2026-03-duplicate.pdf`
- Out of period:
  `source-documents/invoices/invoice-april-generator-overhaul-2026-04.pdf`
  `source-documents/invoices/invoice-april-generator-overhaul-2026-04.xlsx`

### Clean-close collection actions

To create a clean imported-books close run:

1. Reject, delete, or otherwise resolve the duplicate PDF.
2. Reject or leave aside the April invoice as out-of-period.
3. Approve the remaining in-period invoices, payslips, contracts, and bank statements.
4. If the scanned statement parses weakly, use reparse after OCR runtime is available.

Collection should become ready after review blockers are cleared.

## Processing-phase test

### Expected recommendation behavior

Bank statements and contracts should not create GL-coding recommendations.

These invoices are intentionally already represented in the imported GL and
should be suppressed from duplicate posting:

{represented_invoices}

These invoices are intentionally not represented in the imported GL and should
remain eligible for recommendation generation:

{unrepresented_invoices}

### Payslip behavior

The pack includes detailed payslips, but the imported GL carries payroll at
batch level, not per employee. That means payslips remain useful for testing
document parsing and recommendation behavior instead of being silently
suppressed.

### What to verify

- represented invoices do not re-enter GL coding
- unrepresented invoices can still produce recommendations
- payslips can still generate payroll-related recommendations if your workflow allows them
- bank statements remain evidence-only
- contracts remain evidence/context documents, not GL-coding sources

## Reconciliation-phase test

### Ledger-side inputs

This pack includes both sides needed for real bank reconciliation:

- bank statements in `source-documents/bank-statements/`
- imported GL in `ledger/apex-meridian-general-ledger-2026-03.xlsx`

### What to verify

- the close run is in `imported_general_ledger` mode once GL is bound
- bank reconciliation is applicable
- the operating account uses account code `1010`
- the payroll account uses account code `1015`
- the digital bank statement PDFs and XLSX files both reconcile against the imported books
- the scanned operating statement can be reviewed as an OCR scenario

### Important note

If OCR runtime is not installed, the scanned PDF is still useful for verifying
fail-fast behavior and manual reparse workflows, but the digital bank statement
files remain the canonical reconciliation test.

## Reporting-phase test

### Deterministic outputs that should be available

- profit and loss
- trial balance based control view
- KPI dashboard
- cash flow
- effective general-ledger export

### What will not be fully populated by this pack alone

- budget-vs-actual requires a populated budget supporting schedule inside the app

### What to verify

- report generation succeeds with imported GL and TB present
- KPI values and cash flow are built from deterministic data
- commentary, if enabled, is additive narrative on top of deterministic numbers
- the GL export includes imported lines plus any approved close-run adjustments

## Optional admin and workflow regression checks

- Reparse the scanned bank statement
- Delete one uploaded document while still in Collection
- Delete and recreate the close run
- Regenerate recommendations after a review change
- Delete chat threads created during the exercise

## Pass criteria

- COA import succeeds
- GL import succeeds and binds to the close run
- TB import succeeds and binds to the close run
- All in-period source documents parse into the correct document classes
- Duplicate and out-of-period exceptions are surfaced truthfully
- Represented invoices are suppressed from duplicate posting
- Unrepresented invoices remain eligible for recommendation generation
- Bank reconciliation is available and truthfully tied to ledger-side data
- Reports and exports generate from deterministic close-run data

## Suggested execution pattern

1. Run a strict clean-close path first.
2. Recreate a second run and intentionally leave the duplicate and April
   invoice unresolved to confirm Collection blocking.
3. Recreate a third run using only source documents to compare behavior against imported-books mode.
"""
    (PACK_DIR / "FULL_PLATFORM_TEST_SUITE.md").write_text(content, encoding="utf-8")


def create_chat_agent_prompt_guide() -> None:
    represented_invoice_slugs = ", ".join(
        invoice.slug
        for invoice in INVOICES
        if invoice.represented_in_gl and invoice.invoice_date <= CLOSE_PERIOD_END
    )
    unrepresented_invoice_slugs = ", ".join(
        invoice.slug
        for invoice in INVOICES
        if not invoice.represented_in_gl and invoice.invoice_date <= CLOSE_PERIOD_END
    )
    content = f"""# Chat Agent Platform Test Prompts

Use these prompts in the AI workbench / chat surface to run a structured
acceptance test against the enterprise pack.

## Before you start

Use these prompts after you have:

1. created the entity / workspace
2. created the March 2026 close run
3. uploaded the COA, GL, TB, and source documents from this pack

The recommended close period is `{CLOSE_PERIOD_START.isoformat()}` to
`{CLOSE_PERIOD_END.isoformat()}`.

## Prompt 1: Initial audit

Paste this first:

```text
You are helping me run a full platform acceptance test for the Apex Meridian
Distribution Limited March 2026 close run.

First inspect the current workspace and close-run state. Do not mutate anything yet.

I want you to:
1. identify the operating mode
2. list every blocker by phase
3. summarize the uploaded documents by document type
4. call out duplicates, out-of-period items, and OCR-required items
5. tell me whether the imported COA, GL, and TB are present and bound
6. produce a phased test checklist for Collection, Processing, Reconciliation,
   Reporting, and Review/Sign-off

Be explicit and concrete. Use the close-run state as it exists rather than assumptions.
```

## Prompt 2: Collection resolution

Use this after the initial audit:

```text
Guide me through Collection for a clean imported-books close.

Based on the currently uploaded Apex Meridian pack, tell me exactly which documents should be:
- approved
- rejected
- deleted
- reparsed

Call out the expected duplicate invoice, the expected out-of-period April
invoice, and the scanned bank statement separately.

Do not advance phases yet. I want the precise review actions first.
```

## Prompt 3: Processing expectations

Use this once Collection is clean:

```text
Now inspect the approved March 2026 documents and explain what should happen in Processing.

Specifically:
1. tell me which approved documents should not generate GL-coding recommendations
2. tell me which invoices should be suppressed because they are already
   represented in the imported GL
3. tell me which invoices should remain eligible because they are not
   represented in the imported GL
4. tell me how the payslips should behave in this pack
5. tell me what recommendation queue I should expect before I run anything

Use these known represented invoice slugs as a reference set:
{represented_invoice_slugs}

Use these known unrepresented invoice slugs as a reference set:
{unrepresented_invoice_slugs}
```

## Prompt 4: Recommendation run

Use this when you want the chat agent to drive Processing:

```text
Proceed with Processing for the clean March 2026 Apex Meridian close run.

Generate only the recommendations that should truthfully exist based on the
approved documents and imported GL baseline.

Then summarize:
- what was generated
- what was intentionally suppressed
- what still needs human review
- whether anything looks inconsistent with the imported-books operating mode
```

## Prompt 5: Reconciliation run

Use this after Processing is in a good state:

```text
Inspect Reconciliation for the Apex Meridian March 2026 close run and explain
whether bank reconciliation is applicable.

If it is applicable, run or guide the bank reconciliation and summarize:
1. which bank accounts are being reconciled
2. how many statement sources are available
3. what matched cleanly
4. what unmatched items remain
5. whether any unmatched items are expected from this fixture pack

If anything is blocked, explain the blocker concretely instead of giving a generic answer.
```

## Prompt 6: Reporting and exports

Use this after reconciliation is ready or complete:

```text
Inspect Reporting for the Apex Meridian March 2026 close run.

Tell me:
1. which report sections are deterministically available now
2. whether trial-balance-backed reporting is available
3. whether KPI dashboard and cash flow should generate from the current data
4. what budget-vs-actual limitations remain if no supporting schedule has been populated
5. what exports are available, including the effective general-ledger export

Then guide me through the final reporting test sequence.
```

## Prompt 7: Final QA summary

Use this at the end:

```text
Produce a final acceptance-test report for the Apex Meridian March 2026 close run.

I want:
1. pass / fail by workflow phase
2. a list of issues found
3. a list of expected fixture exceptions versus actual product defects
4. a short release-readiness summary
5. recommended follow-up tests, including one source-documents-only run and one OCR-focused rerun
```

## Optional destructive/admin prompt

Use this only if you want to exercise cleanup behavior:

```text
Help me run cleanup and admin regression tests for this close run.

I want to test:
- document reparse
- document delete
- close-run delete
- chat-thread delete

Inspect current state first, then tell me the safest order to run those tests
without confusing the main close-run results.
```
"""
    (PACK_DIR / "CHAT_AGENT_PLATFORM_TEST_PROMPTS.md").write_text(content, encoding="utf-8")


def create_manual_ui_qa_checklist() -> None:
    content = f"""# Manual UI QA Checklist

Use this checklist when you want to test the full platform manually through the
UI instead of driving the flow mainly through chat.

## Test target

- Entity / workspace: `{COMPANY_NAME}`
- Base currency: `{BASE_CURRENCY}`
- Close period: `{CLOSE_PERIOD_START.isoformat()}` to
  `{CLOSE_PERIOD_END.isoformat()}`

## Pack files to use

- COA: `coa/apex-meridian-enterprise-coa.xlsx`
- GL: `ledger/apex-meridian-general-ledger-2026-03.xlsx`
- TB: `ledger/apex-meridian-trial-balance-2026-03.xlsx`
- Source documents: everything under `source-documents/`

## Expected baseline facts

- [ ] Source document count is `27`
- [ ] In-period invoice total is `102,800,000.00`
- [ ] Represented-in-GL invoice total is `87,320,000.00`
- [ ] Unrepresented invoice total is `15,480,000.00`
- [ ] Payroll net total is `10,970,000.00`
- [ ] GL line count is `46`
- [ ] Trial balance row count is `40`

## Setup

- [ ] Create a new entity named `{COMPANY_NAME}`
- [ ] Confirm base currency is `{BASE_CURRENCY}`
- [ ] Create a close run for `{CLOSE_PERIOD_START.isoformat()}` to
      `{CLOSE_PERIOD_END.isoformat()}`
- [ ] Confirm the close run opens in `Collection`

## Import books baseline

- [ ] Upload the COA workbook
- [ ] Confirm COA import succeeds
- [ ] Upload the GL workbook
- [ ] Confirm GL import succeeds
- [ ] Upload the TB workbook
- [ ] Confirm TB import succeeds
- [ ] Confirm the close run shows imported-ledger / imported-books mode

## Upload source documents

- [ ] Upload every file under `source-documents/`
- [ ] Confirm invoices classify as `invoice`
- [ ] Confirm payslips classify as `payslip`
- [ ] Confirm contracts classify as `contract`
- [ ] Confirm bank statements classify as `bank_statement`
- [ ] Confirm the scanned operating bank statement is OCR-routed or clearly
      marked as requiring OCR

## Collection review

- [ ] Find the duplicate document:
      `invoice-kano-field-marketing-2026-03-duplicate.pdf`
- [ ] Confirm it is flagged as a duplicate candidate
- [ ] Find the April invoice files:
      `invoice-april-generator-overhaul-2026-04.pdf` and
      `invoice-april-generator-overhaul-2026-04.xlsx`
- [ ] Confirm they are flagged as outside the March close period
- [ ] Approve the clean in-period invoices
- [ ] Approve the clean in-period payslips
- [ ] Approve the contracts
- [ ] Approve the digital bank statements
- [ ] Reparse the scanned bank statement if OCR is available, otherwise leave it
      as an OCR/fail-fast scenario
- [ ] Resolve the duplicate document so it no longer blocks Collection
- [ ] Reject, ignore, or otherwise leave aside the April invoice as out of
      period
- [ ] Confirm Collection becomes ready

## Advance to Processing

- [ ] Advance the close run to `Processing`
- [ ] Confirm the run is not blocked by bank statements or contracts trying to
      enter GL coding

## Processing review

- [ ] Confirm these represented invoices are suppressed from duplicate posting:
      `invoice-harbor-warehouse-rent-2026-03`
- [ ] Confirm `invoice-nova-diesel-supply-2026-03` is suppressed
- [ ] Confirm `invoice-axis-haulage-2026-03` is suppressed
- [ ] Confirm `invoice-signal-security-services-2026-03` is suppressed
- [ ] Confirm `invoice-cloud-erp-subscription-2026-03` is suppressed
- [ ] Confirm `invoice-kano-field-marketing-2026-03` is suppressed
- [ ] Confirm `invoice-vertex-forklifts-2026-03` is suppressed
- [ ] Confirm `invoice-datahub-connectivity-2026-03` is suppressed
- [ ] Confirm these invoices remain eligible for recommendation generation:
      `invoice-oceanic-audit-retainer-2026-03`
- [ ] Confirm `invoice-north-gate-customs-clearing-2026-03` remains eligible
- [ ] Confirm bank statements do not generate GL-coding recommendations
- [ ] Confirm contracts do not generate GL-coding recommendations
- [ ] Confirm payslips behave as reviewable payroll-source documents
- [ ] Generate recommendations
- [ ] Confirm only truthfully eligible recommendations are created

## Journal handling

- [ ] Review recommendation confidence and reasoning
- [ ] Approve only the recommendations that look correct
- [ ] Confirm journals are created only from approved recommendation paths
- [ ] Confirm imported-GL represented invoices were not re-booked into journals

## Reconciliation

- [ ] Advance to `Reconciliation`
- [ ] Confirm bank reconciliation is applicable because GL baseline exists
- [ ] Confirm the operating account is tied to account code `1010`
- [ ] Confirm the payroll account is tied to account code `1015`
- [ ] Run reconciliation
- [ ] Confirm digital bank statements produce truthful reconciliation activity
- [ ] Review unmatched items, if any, and verify they are explainable from the
      fixture data rather than parser errors
- [ ] Confirm the scanned statement is treated as an OCR scenario rather than
      the canonical reconciliation source if OCR is unavailable

## Reporting

- [ ] Advance to `Reporting`
- [ ] Generate reports
- [ ] Confirm profit and loss is available
- [ ] Confirm trial-balance-backed reporting is available
- [ ] Confirm KPI dashboard is available
- [ ] Confirm cash flow is available
- [ ] Confirm commentary is additive and does not replace deterministic numbers
- [ ] Confirm any missing budget-vs-actual detail is due to schedule population,
      not a reporting failure

## Exports

- [ ] Open the exports area
- [ ] Generate the effective general-ledger export
- [ ] Confirm the export includes imported baseline rows plus close-run
      adjustments
- [ ] Generate the report pack export if available
- [ ] Confirm export download links work

## Agent / chat cross-check

- [ ] Use `CHAT_AGENT_PLATFORM_TEST_PROMPTS.md` to run the same close through
      chat
- [ ] Confirm the chat agent correctly identifies operating mode
- [ ] Confirm the chat agent calls out duplicate and out-of-period documents
- [ ] Confirm the chat agent explains duplicate-post suppression accurately
- [ ] Confirm the chat agent describes reporting availability truthfully

## Optional destructive tests

- [ ] Reparse the scanned bank statement
- [ ] Delete a document while the run is still in `Collection`
- [ ] Regenerate recommendations after a review change
- [ ] Delete a chat thread
- [ ] Delete and recreate the close run

## Final sign-off

- [ ] All intentional fixture exceptions are understood and documented
- [ ] No unexpected blockers remain
- [ ] No represented invoice was double-booked
- [ ] Reconciliation behavior matches the imported-ledger mode design
- [ ] Reporting and exports complete successfully
- [ ] The platform is ready for a final release-readiness judgment
"""
    (PACK_DIR / "MANUAL_UI_QA_CHECKLIST.md").write_text(content, encoding="utf-8")


def create_all_documents() -> None:
    for invoice in INVOICES:
        create_invoice_pdf(INVOICES_DIR / f"{invoice.slug}.pdf", invoice)
        create_invoice_workbook(INVOICES_DIR / f"{invoice.slug}.xlsx", invoice)

    duplicate_source = INVOICES_DIR / "invoice-kano-field-marketing-2026-03.pdf"
    shutil.copyfile(
        duplicate_source,
        INVOICES_DIR / "invoice-kano-field-marketing-2026-03-duplicate.pdf",
    )

    for payslip in PAYSLIPS:
        create_payslip_pdf(PAYSLIPS_DIR / f"{payslip.slug}.pdf", payslip)
        create_payslip_workbook(PAYSLIPS_DIR / f"{payslip.slug}.xlsx", payslip)

    for contract in CONTRACTS:
        create_contract_pdf(CONTRACTS_DIR / f"{contract.slug}.pdf", contract)
        create_contract_workbook(CONTRACTS_DIR / f"{contract.slug}.xlsx", contract)

    for statement in BANK_STATEMENTS:
        create_bank_statement_pdf(BANK_STATEMENTS_DIR / f"{statement.slug}.pdf", statement)
        create_bank_statement_workbook(BANK_STATEMENTS_DIR / f"{statement.slug}.xlsx", statement)
        if statement.scanned_pdf:
            create_scanned_bank_statement_pdf(
                BANK_STATEMENTS_DIR / f"{statement.slug}-scanned.pdf",
                statement,
            )

    create_coa_workbook(COA_DIR / "apex-meridian-enterprise-coa.xlsx")
    create_coa_pdf(COA_DIR / "apex-meridian-enterprise-coa.pdf")
    create_general_ledger_workbook(LEDGER_DIR / "apex-meridian-general-ledger-2026-03.xlsx")
    create_general_ledger_pdf(LEDGER_DIR / "apex-meridian-general-ledger-2026-03.pdf")
    create_trial_balance_workbook(LEDGER_DIR / "apex-meridian-trial-balance-2026-03.xlsx")
    create_trial_balance_pdf(LEDGER_DIR / "apex-meridian-trial-balance-2026-03.pdf")
    create_manifest()
    create_readme()
    create_full_platform_test_suite()
    create_chat_agent_prompt_guide()
    create_manual_ui_qa_checklist()


def main() -> None:
    ensure_directories()
    create_all_documents()
    print(f"Generated enterprise close fixture pack at {PACK_DIR}")


if __name__ == "__main__":
    main()
