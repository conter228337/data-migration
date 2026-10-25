"""Clean dirty CSV exports and load them into a relational database.

Usage:
    python migrate.py --customers data/customers_dirty.csv \
                      --orders data/orders_dirty.csv \
                      --db output/clean.db --rejects output/rejects.csv

Rules (see README for the reasoning):
  * Rows are never dropped silently: every rejected row is kept with a reason.
  * Customers are de-duplicated by normalised e-mail; duplicate ids are
    recorded in customer_aliases and orders are re-pointed to the survivor.
  * Money is stored as integer cents, never floats.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Slash dates are read as MM/DD/YYYY and dotted dates as DD.MM.YYYY.
# This is an assumption; change it here if your source data differs.
DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d.%m.%Y",
    "%m/%d/%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%B %d, %Y",
)

EMAIL_RE = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$")

COUNTRY_ALIASES = {
    "us": "United States",
    "usa": "United States",
    "u.s.a.": "United States",
    "united states": "United States",
    "united states of america": "United States",
    "uk": "United Kingdom",
    "gb": "United Kingdom",
    "great britain": "United Kingdom",
    "united kingdom": "United Kingdom",
    "de": "Germany",
    "germany": "Germany",
    "deutschland": "Germany",
    "fr": "France",
    "france": "France",
    "pl": "Poland",
    "poland": "Poland",
    "es": "Spain",
    "spain": "Spain",
}

STATUS_ALIASES = {
    "paid": "paid",
    "complete": "paid",
    "completed": "paid",
    "pending": "pending",
    "refunded": "refunded",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}


# --------------------------------------------------------------------------
# Field-level cleaners
# --------------------------------------------------------------------------
def parse_date(raw: str | None) -> date | None:
    text = (raw or "").strip()
    if not text:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def normalize_email(raw: str | None) -> str | None:
    """Lower-case and trim; return None when empty or malformed."""
    email = (raw or "").strip().lower()
    return email if EMAIL_RE.match(email) else None


def normalize_phone(raw: str | None) -> str | None:
    """Keep digits (and a leading +). Return None if not 7-15 digits."""
    text = (raw or "").strip()
    if not text:
        return None
    digits = re.sub(r"\D", "", text)
    if not 7 <= len(digits) <= 15:
        return None
    return ("+" if text.startswith("+") else "") + digits


def normalize_country(raw: str | None) -> str | None:
    text = " ".join((raw or "").split())
    if not text:
        return None
    return COUNTRY_ALIASES.get(text.lower(), text.title())


def parse_amount_cents(raw: str | None) -> int | None:
    """Turn '$1,234.50', '1.234,50 EUR', '12,5', '(40.00)' into integer cents.

    Returns None when no number can be read. A leading '-' or parentheses
    give a negative value so the caller can reject it explicitly.
    """
    text = (raw or "").strip()
    if not text:
        return None
    negative = text.startswith("-") or (text.startswith("(") and text.endswith(")"))
    text = re.sub(r"[^\d.,]", "", text)
    if not re.search(r"\d", text):
        return None

    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):      # 1.234,50 -> European
            text = text.replace(".", "").replace(",", ".")
        else:                                       # 1,234.50 -> US
            text = text.replace(",", "")
    elif "," in text:
        head, _, tail = text.rpartition(",")
        if len(tail) in (1, 2):                     # 12,5 / 12,50 -> decimal
            text = head.replace(",", "") + "." + tail
        else:                                       # 1,234 -> thousands
            text = text.replace(",", "")

    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    cents = int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    return -cents if negative else cents


# --------------------------------------------------------------------------
# Row-level pipeline (pure functions, no I/O)
# --------------------------------------------------------------------------
@dataclass
class Customer:
    customer_id: int
    full_name: str
    email: str
    phone: str | None
    country: str | None
    signup_date: date


@dataclass
class Order:
    order_id: int
    customer_id: int
    order_date: date
    amount_cents: int
    status: str


@dataclass
class Reject:
    source: str
    row: int
    reason: str
    raw: dict


@dataclass
class CleanResult:
    customers: list[Customer] = field(default_factory=list)
    aliases: dict[int, int] = field(default_factory=dict)
    orders: list[Order] = field(default_factory=list)
    rejects: list[Reject] = field(default_factory=list)
    input_rows: Counter = field(default_factory=Counter)
    duplicates_merged: int = 0
    phones_dropped: int = 0


def _parse_int(raw: str | None) -> int | None:
    try:
        return int((raw or "").strip())
    except ValueError:
        return None


def clean_customers(rows: list[tuple[int, dict]], as_of: date, result: CleanResult) -> None:
    by_email: dict[str, Customer] = {}
    seen_ids: set[int] = set()
    result.input_rows["customers"] += len(rows)

    def reject(rownum: int, reason: str, row: dict) -> None:
        result.rejects.append(Reject("customers", rownum, reason, row))

    for rownum, row in rows:
        cid = _parse_int(row.get("customer_id"))
        if cid is None:
            reject(rownum, "invalid_customer_id", row)
            continue

        name = " ".join((row.get("full_name") or "").split())
        if not name:
            reject(rownum, "missing_name", row)
            continue

        if not (row.get("email") or "").strip():
            reject(rownum, "missing_email", row)
            continue
        email = normalize_email(row.get("email"))
        if email is None:
            reject(rownum, "invalid_email", row)
            continue

        signup = parse_date(row.get("signup_date"))
        if signup is None:
            reject(rownum, "invalid_signup_date", row)
            continue
        if signup > as_of:
            reject(rownum, "future_signup_date", row)
            continue

        phone_raw = (row.get("phone") or "").strip()
        phone = normalize_phone(phone_raw)
        if phone_raw and phone is None:
            result.phones_dropped += 1
        country = normalize_country(row.get("country"))

        survivor = by_email.get(email)
        if survivor is not None:
            # Same person under another id: merge, keep the earliest signup,
            # and fill gaps from the duplicate.
            survivor.phone = survivor.phone or phone
            survivor.country = survivor.country or country
            survivor.signup_date = min(survivor.signup_date, signup)
            result.aliases[cid] = survivor.customer_id
            seen_ids.add(cid)
            result.duplicates_merged += 1
            continue

        if cid in seen_ids:
            reject(rownum, "duplicate_customer_id", row)
            continue

        customer = Customer(cid, name, email, phone, country, signup)
        by_email[email] = customer
        seen_ids.add(cid)
        result.aliases[cid] = cid
        result.customers.append(customer)


def clean_orders(rows: list[tuple[int, dict]], as_of: date, result: CleanResult) -> None:
    seen_orders: set[int] = set()
    result.input_rows["orders"] += len(rows)

    def reject(rownum: int, reason: str, row: dict) -> None:
        result.rejects.append(Reject("orders", rownum, reason, row))

    for rownum, row in rows:
        oid = _parse_int(row.get("order_id"))
        if oid is None:
            reject(rownum, "invalid_order_id", row)
            continue
        if oid in seen_orders:
            reject(rownum, "duplicate_order_id", row)
            continue

        source_cid = _parse_int(row.get("customer_id"))
        cid = result.aliases.get(source_cid) if source_cid is not None else None
        if cid is None:
            reject(rownum, "unknown_customer", row)
            continue

        order_date = parse_date(row.get("order_date"))
        if order_date is None:
            reject(rownum, "invalid_order_date", row)
            continue
        if order_date > as_of:
            reject(rownum, "future_order_date", row)
            continue

        cents = parse_amount_cents(row.get("amount"))
        if cents is None:
            reject(rownum, "invalid_amount", row)
            continue
        if cents <= 0:
            reject(rownum, "non_positive_amount", row)
            continue

        status = STATUS_ALIASES.get((row.get("status") or "").strip().lower())
        if status is None:
            reject(rownum, "invalid_status", row)
            continue

        seen_orders.add(oid)
        result.orders.append(Order(oid, cid, order_date, cents, status))


def clean(customer_rows, order_rows, as_of: date | None = None) -> CleanResult:
    as_of = as_of or date.today()
    result = CleanResult()
    clean_customers(customer_rows, as_of, result)
    clean_orders(order_rows, as_of, result)   # needs aliases from customers
    return result


# --------------------------------------------------------------------------
# I/O: CSV in, database + reject file + report out
# --------------------------------------------------------------------------
def read_csv(path: Path) -> list[tuple[int, dict]]:
    with open(path, newline="", encoding="utf-8-sig") as handle:
        return [(n, row) for n, row in enumerate(csv.DictReader(handle), start=2)]


def load_database(result: CleanResult, db_path: Path, schema_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(schema_path.read_text(encoding="utf-8"))
        with conn:  # one transaction: all or nothing
            conn.executemany(
                "INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?)",
                [(c.customer_id, c.full_name, c.email, c.phone, c.country,
                  c.signup_date.isoformat()) for c in result.customers],
            )
            conn.executemany(
                "INSERT INTO customer_aliases VALUES (?, ?)",
                sorted(result.aliases.items()),
            )
            conn.executemany(
                "INSERT INTO orders VALUES (?, ?, ?, ?, ?)",
                [(o.order_id, o.customer_id, o.order_date.isoformat(),
                  o.amount_cents, o.status) for o in result.orders],
            )
            conn.executemany(
                "INSERT INTO rejected_rows VALUES (?, ?, ?, ?)",
                [(r.source, r.row, r.reason, json.dumps(r.raw, ensure_ascii=False))
                 for r in result.rejects],
            )
    finally:
        conn.close()


def write_rejects(result: CleanResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["source", "source_row", "reason", "raw_data"])
        for r in result.rejects:
            writer.writerow([r.source, r.row, r.reason,
                             json.dumps(r.raw, ensure_ascii=False)])


def format_report(result: CleanResult) -> str:
    by_reason = {"customers": Counter(), "orders": Counter()}
    for r in result.rejects:
        by_reason[r.source][r.reason] += 1

    lines = ["MIGRATION REPORT", "=" * 40]
    loaded = {"customers": len(result.customers), "orders": len(result.orders)}
    for source in ("customers", "orders"):
        total = result.input_rows[source]
        rejected = sum(by_reason[source].values())
        merged = result.duplicates_merged if source == "customers" else 0
        lines.append(f"\n{source.upper()}")
        lines.append(f"  rows read:        {total}")
        lines.append(f"  loaded:           {loaded[source]}")
        if source == "customers":
            lines.append(f"  merged duplicates:{merged:>4}")
        lines.append(f"  rejected:         {rejected}")
        for reason, count in sorted(by_reason[source].items()):
            lines.append(f"    - {reason}: {count}")
        check = loaded[source] + merged + rejected
        lines.append(f"  reconciliation:   {loaded[source]} + {merged} + {rejected} = {check}"
                     f" ({'OK' if check == total else 'MISMATCH'})")
    lines.append(f"\nPhone numbers dropped as invalid (row kept): {result.phones_dropped}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--customers", type=Path, required=True)
    parser.add_argument("--orders", type=Path, required=True)
    parser.add_argument("--db", type=Path, default=HERE / "output" / "clean.db")
    parser.add_argument("--rejects", type=Path, default=HERE / "output" / "rejects.csv")
    parser.add_argument("--schema", type=Path, default=HERE / "schema.sql")
    parser.add_argument("--as-of", type=date.fromisoformat, default=None,
                        help="reject dates after this day (YYYY-MM-DD); default today")
    args = parser.parse_args(argv)

    result = clean(read_csv(args.customers), read_csv(args.orders), args.as_of)
    load_database(result, args.db, args.schema)
    write_rejects(result, args.rejects)
    print(format_report(result))
    print(f"\nDatabase:  {args.db}\nRejects:   {args.rejects}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
