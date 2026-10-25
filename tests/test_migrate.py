import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import generate_dirty_data  # noqa: E402
import migrate  # noqa: E402

AS_OF = date(2026, 1, 1)


# ---------------------------------------------------------------- cleaners
@pytest.mark.parametrize("raw, expected", [
    ("12.50", 1250),
    ("$1,234.50", 123450),
    ("1,234.50 USD", 123450),
    ("€1.234,50", 123450),
    ("12,5", 1250),
    ("12,50", 1250),
    ("1,234", 123400),
    ("(40.00)", -4000),
    ("-25.00", -2500),
    ("0", 0),
    ("0.005", 1),          # half-up rounding
    ("N/A", None),
    ("", None),
    (None, None),
    ("1.234.567", None),   # ambiguous, refuse to guess
])
def test_parse_amount_cents(raw, expected):
    assert migrate.parse_amount_cents(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("2024-03-05", date(2024, 3, 5)),
    ("05.03.2024", date(2024, 3, 5)),     # dotted = DD.MM.YYYY
    ("03/05/2024", date(2024, 3, 5)),     # slash  = MM/DD/YYYY
    ("05 Mar 2024", date(2024, 3, 5)),
    ("31.02.2023", None),
    ("yesterday", None),
    ("", None),
    (None, None),
])
def test_parse_date(raw, expected):
    assert migrate.parse_date(raw) == expected


def test_normalize_email():
    assert migrate.normalize_email("  Anna.Meyer@Example.COM ") == "anna.meyer@example.com"
    for bad in ["", "plain", "a@b", "@example.com", "x y@example.com", "john@"]:
        assert migrate.normalize_email(bad) is None


def test_normalize_phone():
    assert migrate.normalize_phone("+49 (170) 123-4567") == "+491701234567"
    assert migrate.normalize_phone("0170 1234567") == "01701234567"
    assert migrate.normalize_phone("123") is None
    assert migrate.normalize_phone("") is None


def test_normalize_country():
    assert migrate.normalize_country(" usa ") == "United States"
    assert migrate.normalize_country("deutschland") == "Germany"
    assert migrate.normalize_country("narnia") == "Narnia"
    assert migrate.normalize_country("") is None


# ---------------------------------------------------------------- pipeline
def rows(*dicts):
    return [(n, d) for n, d in enumerate(dicts, start=2)]


def cust(cid, email, **extra):
    base = {"customer_id": str(cid), "full_name": "Test User", "email": email,
            "phone": "", "country": "", "signup_date": "2024-01-10"}
    base.update(extra)
    return base


def order(oid, cid, **extra):
    base = {"order_id": str(oid), "customer_id": str(cid),
            "order_date": "2025-02-01", "amount": "10.00", "status": "paid"}
    base.update(extra)
    return base


def test_duplicate_customers_are_merged_and_fields_filled():
    result = migrate.clean(
        rows(cust(1, "a@example.com", signup_date="2024-05-01"),
             cust(2, " A@EXAMPLE.COM ", phone="+49 170 1234567", country="de",
                  signup_date="2024-02-01")),
        rows(), AS_OF)
    assert len(result.customers) == 1
    survivor = result.customers[0]
    assert survivor.customer_id == 1
    assert survivor.phone == "+491701234567"       # filled from duplicate
    assert survivor.country == "Germany"
    assert survivor.signup_date == date(2024, 2, 1)  # earliest kept
    assert result.aliases == {1: 1, 2: 1}
    assert result.duplicates_merged == 1


def test_orders_are_repointed_to_surviving_customer():
    result = migrate.clean(
        rows(cust(1, "a@example.com"), cust(2, "A@example.com")),
        rows(order(10, 2)), AS_OF)
    assert [o.customer_id for o in result.orders] == [1]


def test_customer_rejects_have_reasons():
    result = migrate.clean(
        rows(cust("x", "a@example.com"),
             cust(2, ""),
             cust(3, "bad@"),
             cust(4, "d@example.com", full_name="  "),
             cust(5, "e@example.com", signup_date="31.02.2023"),
             cust(6, "f@example.com", signup_date="2030-01-01"),
             cust(7, "g@example.com"),
             cust(7, "h@example.com")),
        rows(), AS_OF)
    reasons = [r.reason for r in result.rejects]
    assert reasons == ["invalid_customer_id", "missing_email", "invalid_email",
                       "missing_name", "invalid_signup_date", "future_signup_date",
                       "duplicate_customer_id"]
    assert [c.customer_id for c in result.customers] == [7]


def test_order_rejects_have_reasons():
    result = migrate.clean(
        rows(cust(1, "a@example.com")),
        rows(order(1, 1),                              # ok
             order(1, 1),                              # duplicate id
             order(2, 999),                            # orphan
             order(3, 1, order_date="2099-01-01"),     # future
             order(4, 1, order_date="nope"),           # bad date
             order(5, 1, amount="N/A"),                # bad amount
             order(6, 1, amount="-5.00"),              # negative
             order(7, 1, status="lost"),               # bad status
             order("x", 1)),                           # bad id
        AS_OF)
    assert [o.order_id for o in result.orders] == [1]
    assert [r.reason for r in result.rejects] == [
        "duplicate_order_id", "unknown_customer", "future_order_date",
        "invalid_order_date", "invalid_amount", "non_positive_amount",
        "invalid_status", "invalid_order_id"]


def test_first_copy_rejected_does_not_block_a_valid_later_copy():
    result = migrate.clean(
        rows(cust(1, "a@example.com")),
        rows(order(1, 1, amount="N/A"), order(1, 1, amount="5.00")), AS_OF)
    assert [(o.order_id, o.amount_cents) for o in result.orders] == [(1, 500)]


# ------------------------------------------------------------- end to end
def test_end_to_end_on_generated_data(tmp_path):
    counts = generate_dirty_data.generate(tmp_path / "data", seed=42)
    db = tmp_path / "out" / "clean.db"
    rejects = tmp_path / "out" / "rejects.csv"

    code = migrate.main([
        "--customers", str(tmp_path / "data" / "customers_dirty.csv"),
        "--orders", str(tmp_path / "data" / "orders_dirty.csv"),
        "--db", str(db), "--rejects", str(rejects), "--as-of", "2026-01-01"])
    assert code == 0

    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = ON")
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731

    # referential integrity and constraints hold
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert one("SELECT COUNT(*) FROM customers WHERE email != lower(trim(email))") == 0
    assert one("SELECT COUNT(*) FROM orders WHERE amount_cents <= 0") == 0

    # every input row is accounted for: loaded + merged + rejected = read
    loaded_c = one("SELECT COUNT(*) FROM customers")
    merged = one("SELECT COUNT(*) FROM customer_aliases WHERE source_customer_id != customer_id")
    rejected_c = one("SELECT COUNT(*) FROM rejected_rows WHERE source='customers'")
    assert loaded_c + merged + rejected_c == counts["customers"]

    loaded_o = one("SELECT COUNT(*) FROM orders")
    rejected_o = one("SELECT COUNT(*) FROM rejected_rows WHERE source='orders'")
    assert loaded_o + rejected_o == counts["orders"]

    # the planted problems were actually caught
    reasons = {r[0] for r in conn.execute("SELECT DISTINCT reason FROM rejected_rows")}
    assert {"missing_email", "invalid_email", "unknown_customer", "duplicate_order_id",
            "non_positive_amount", "invalid_amount", "invalid_order_date"} <= reasons
    assert merged == 15
    assert rejects.read_text(encoding="utf-8").count("\n") == rejected_c + rejected_o + 1
    conn.close()


def test_generator_is_reproducible(tmp_path):
    generate_dirty_data.generate(tmp_path / "a", seed=7)
    generate_dirty_data.generate(tmp_path / "b", seed=7)
    for name in ("customers_dirty.csv", "orders_dirty.csv"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()
