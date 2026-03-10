#!/usr/bin/env python3
from __future__ import annotations

import csv
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "data" / "banking_csv"


TABLES = {
    "customer_dim": [
        {
            "customer_id": "C001",
            "customer_name": "Alice Johnson",
            "date_of_birth": "1985-04-12",
            "customer_segment": "RETAIL",
            "risk_rating": "LOW",
            "kyc_status": "COMPLETED",
            "email": "alice.johnson@example.com",
        },
        {
            "customer_id": "C002",
            "customer_name": "Bob Smith",
            "date_of_birth": "1978-09-23",
            "customer_segment": "SME",
            "risk_rating": "MEDIUM",
            "kyc_status": "COMPLETED",
            "email": "bob.smith@example.com",
        },
        {
            "customer_id": "C003",
            "customer_name": "Carol Lee",
            "date_of_birth": "1990-01-17",
            "customer_segment": "PRIVATE",
            "risk_rating": "HIGH",
            "kyc_status": "PENDING",
            "email": "carol.lee@example.com",
        },
    ],
    "customer_address": [
        {
            "customer_id": "C001",
            "city": "Charlotte",
            "state": "NC",
            "country": "USA",
            "postal_code": "28202",
        },
        {
            "customer_id": "C002",
            "city": "Atlanta",
            "state": "GA",
            "country": "USA",
            "postal_code": "30303",
        },
        {
            "customer_id": "C003",
            "city": "Chicago",
            "state": "IL",
            "country": "USA",
            "postal_code": "60601",
        },
    ],
    "customer_phone": [
        {"customer_id": "C001", "mobile_phone": "+1-704-555-0101"},
        {"customer_id": "C002", "mobile_phone": "+1-404-555-0102"},
        {"customer_id": "C003", "mobile_phone": "+1-312-555-0103"},
    ],
    "branch_dim": [
        {
            "branch_id": "B001",
            "branch_code": "NYC01",
            "branch_name": "Midtown Branch",
            "city": "New York",
            "state": "NY",
            "region": "NORTHEAST",
        },
        {
            "branch_id": "B002",
            "branch_code": "CLT01",
            "branch_name": "Charlotte Main",
            "city": "Charlotte",
            "state": "NC",
            "region": "SOUTHEAST",
        },
        {
            "branch_id": "B003",
            "branch_code": "CHI01",
            "branch_name": "Chicago Loop",
            "city": "Chicago",
            "state": "IL",
            "region": "MIDWEST",
        },
    ],
    "employee_dim": [
        {
            "employee_id": "E001",
            "employee_name": "Rita Gomez",
            "role": "RELATIONSHIP_MANAGER",
            "region": "SOUTHEAST",
            "branch_id": "B002",
        },
        {
            "employee_id": "E002",
            "employee_name": "Daniel Wu",
            "role": "RELATIONSHIP_MANAGER",
            "region": "NORTHEAST",
            "branch_id": "B001",
        },
        {
            "employee_id": "E003",
            "employee_name": "Sonia Patel",
            "role": "LOAN_OFFICER",
            "region": "MIDWEST",
            "branch_id": "B003",
        },
    ],
    "product_dim": [
        {
            "product_id": "P001",
            "product_code": "SAV-STD",
            "product_name": "Standard Savings",
            "product_type": "DEPOSIT",
            "interest_rate": "1.25",
        },
        {
            "product_id": "P002",
            "product_code": "CHK-PRM",
            "product_name": "Premium Checking",
            "product_type": "DEPOSIT",
            "interest_rate": "0.15",
        },
        {
            "product_id": "P003",
            "product_code": "LN-HME",
            "product_name": "Home Lending",
            "product_type": "LOAN",
            "interest_rate": "6.10",
        },
        {
            "product_id": "P004",
            "product_code": "CRD-GLD",
            "product_name": "Gold Card",
            "product_type": "CARD",
            "interest_rate": "18.90",
        },
    ],
    "account_dim": [
        {
            "account_id": "A100",
            "account_number": "100000001",
            "account_type": "SAVINGS",
            "account_status": "ACTIVE",
            "open_date": "2021-05-12",
            "current_balance": "15420.25",
            "currency_code": "USD",
            "branch_id": "B002",
            "relationship_manager_id": "E001",
            "product_id": "P001",
        },
        {
            "account_id": "A101",
            "account_number": "100000002",
            "account_type": "CHECKING",
            "account_status": "ACTIVE",
            "open_date": "2022-03-01",
            "current_balance": "8450.10",
            "currency_code": "USD",
            "branch_id": "B001",
            "relationship_manager_id": "E002",
            "product_id": "P002",
        },
        {
            "account_id": "A102",
            "account_number": "100000003",
            "account_type": "SAVINGS",
            "account_status": "DORMANT",
            "open_date": "2020-11-19",
            "current_balance": "920.55",
            "currency_code": "USD",
            "branch_id": "B003",
            "relationship_manager_id": "E003",
            "product_id": "P001",
        },
    ],
    "account_customer_bridge": [
        {"customer_id": "C001", "account_id": "A100", "relationship_type": "PRIMARY"},
        {"customer_id": "C002", "account_id": "A101", "relationship_type": "PRIMARY"},
        {"customer_id": "C003", "account_id": "A102", "relationship_type": "PRIMARY"},
        {"customer_id": "C001", "account_id": "A101", "relationship_type": "JOINT"},
    ],
    "card_dim": [
        {
            "card_id": "CARD001",
            "card_number_masked": "****1111",
            "card_type": "DEBIT",
            "card_status": "ACTIVE",
            "expiry_date": "2027-12-31",
            "account_id": "A100",
        },
        {
            "card_id": "CARD002",
            "card_number_masked": "****2222",
            "card_type": "DEBIT",
            "card_status": "ACTIVE",
            "expiry_date": "2028-06-30",
            "account_id": "A101",
        },
        {
            "card_id": "CARD003",
            "card_number_masked": "****3333",
            "card_type": "CREDIT",
            "card_status": "BLOCKED",
            "expiry_date": "2026-09-30",
            "account_id": "A102",
        },
    ],
    "loan_dim": [
        {
            "loan_id": "L001",
            "loan_number": "LN0001",
            "loan_type": "MORTGAGE",
            "principal_amount": "350000.00",
            "outstanding_amount": "298450.75",
            "loan_status": "ACTIVE",
            "customer_id": "C001",
        },
        {
            "loan_id": "L002",
            "loan_number": "LN0002",
            "loan_type": "AUTO",
            "principal_amount": "42000.00",
            "outstanding_amount": "12050.10",
            "loan_status": "ACTIVE",
            "customer_id": "C002",
        },
    ],
    "transaction_fact": [
        {
            "transaction_id": "T001",
            "transaction_code": "TXN0001",
            "transaction_date": "2026-02-10T10:15:00Z",
            "transaction_amount": "250.75",
            "channel": "ATM",
            "transaction_type": "CASH_WITHDRAWAL",
            "debit_credit": "DEBIT",
            "account_id": "A100",
        },
        {
            "transaction_id": "T002",
            "transaction_code": "TXN0002",
            "transaction_date": "2026-02-11T14:42:00Z",
            "transaction_amount": "1250.00",
            "channel": "POS",
            "transaction_type": "PURCHASE",
            "debit_credit": "DEBIT",
            "account_id": "A101",
        },
        {
            "transaction_id": "T003",
            "transaction_code": "TXN0003",
            "transaction_date": "2026-02-12T09:05:00Z",
            "transaction_amount": "3500.00",
            "channel": "ONLINE",
            "transaction_type": "TRANSFER",
            "debit_credit": "DEBIT",
            "account_id": "A100",
        },
        {
            "transaction_id": "T004",
            "transaction_code": "TXN0004",
            "transaction_date": "2026-02-12T18:20:00Z",
            "transaction_amount": "220.40",
            "channel": "POS",
            "transaction_type": "PURCHASE",
            "debit_credit": "DEBIT",
            "account_id": "A102",
        },
    ],
    "beneficiary": [
        {
            "beneficiary_id": "BEN001",
            "beneficiary_name": "Green Supplies LLC",
            "bank_name": "Community Bank",
            "beneficiary_account": "99887766",
            "account_id": "A101",
        },
        {
            "beneficiary_id": "BEN002",
            "beneficiary_name": "Metro Housing Corp",
            "bank_name": "National Bank",
            "beneficiary_account": "44332211",
            "account_id": "A100",
        },
    ],
    "alert_fact": [
        {
            "alert_id": "AL001",
            "alert_code": "AML-1001",
            "alert_type": "AML",
            "severity": "MEDIUM",
            "alert_status": "OPEN",
            "raised_at": "2026-02-13T08:30:00Z",
            "account_id": "A100",
        },
        {
            "alert_id": "AL002",
            "alert_code": "FRA-2010",
            "alert_type": "FRAUD",
            "severity": "HIGH",
            "alert_status": "OPEN",
            "raised_at": "2026-02-13T12:10:00Z",
            "account_id": "A102",
        },
    ],
}


def write_table(name: str, rows: list[dict[str, str]]) -> None:
    if not rows:
        raise ValueError(f"Table '{name}' has no rows")
    file_path = OUTPUT_DIR / f"{name}.csv"
    with file_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for table_name, rows in TABLES.items():
        write_table(table_name, rows)
    print(f"Generated {len(TABLES)} CSV tables in {OUTPUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())