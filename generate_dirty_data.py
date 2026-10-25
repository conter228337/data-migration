"""Generate a reproducible, deliberately messy customers/orders export.

All people are fictional (random name combinations, example.com e-mails).
Usage: python generate_dirty_data.py [output_dir] [seed]
"""
from __future__ import annotations

import csv
import random
import sys
from datetime import date, timedelta
from pathlib import Path

FIRST = ["Anna", "Ben", "Clara", "David", "Elena", "Felix", "Greta", "Hugo",
         "Iris", "Jonas", "Kira", "Leon", "Maya", "Noah", "Olga", "Paul"]
LAST = ["Meyer", "Novak", "Rossi", "Silva", "Kowalski", "Berg", "Lopez",
        "Fischer", "Dubois", "Petrov", "Schmidt", "Moreno"]
COUNTRY_VARIANTS = {
    "United States": ["United States", "USA", "us", "U.S.A.", " united states "],
    "United Kingdom": ["United Kingdom", "UK", "gb", "Great Britain"],
    "Germany": ["Germany", "DE", "deutschland"],
    "France": ["France", "FR"],
    "Poland": ["Poland", "pl"],
    "Spain": ["Spain", "ES"],
}
STATUS_VARIANTS = {
    "paid": ["paid", "PAID", "Paid ", "complete"],
    "pending": ["pending", "Pending"],
    "refunded": ["refunded", "Refunded"],
    "cancelled": ["cancelled", "canceled", "CANCELLED"],
}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fmt_date(rng: random.Random, d: date) -> str:
    style = rng.choice(["iso", "dot", "slash", "text", "iso"])
    if style == "iso":
        return d.isoformat()
    if style == "dot":
        return f"{d.day:02d}.{d.month:02d}.{d.year}"
    if style == "slash":
        return f"{d.month:02d}/{d.day:02d}/{d.year}"
    return f"{d.day:02d} {MONTHS[d.month - 1]} {d.year}"


def fmt_amount(rng: random.Random, cents: int) -> str:
    value = cents / 100
    style = rng.choice(["plain", "usd", "code", "eu_comma", "eu_full", "plain"])
    if style == "plain":
        return f"{value:.2f}"
    if style == "usd":
        return f"${value:,.2f}"
    if style == "code":
        return f"{value:,.2f} USD"
    if style == "eu_comma":
        return f"{value:.2f}".replace(".", ",")
    return f"€{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_phone(rng: random.Random, number: str) -> str:
    style = rng.choice(["plain", "spaced", "dashed", "paren"])
    if style == "plain":
        return number
    if style == "spaced":
        return f"+{number[:2]} {number[2:5]} {number[5:8]} {number[8:]}"
    if style == "dashed":
        return f"+{number[:2]}-{number[2:5]}-{number[5:]}"
    return f"+{number[:2]} ({number[2:5]}) {number[5:]}"


def generate(out_dir: Path, seed: int = 42) -> dict:
    rng = random.Random(seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    today = date(2025, 12, 31)

    # ---- base (clean) customers --------------------------------------
    base = []
    for cid in range(1, 201):
        first, last = rng.choice(FIRST), rng.choice(LAST)
        country = rng.choice(list(COUNTRY_VARIANTS))
        base.append({
            "id": cid,
            "name": f"{first} {last}",
            "email": f"{first}.{last}.{cid}@example.com".lower(),
            "phone": "".join(rng.choice("0123456789") for _ in range(11)),
            "country": country,
            "signup": today - timedelta(days=rng.randint(400, 1500)),
        })

    customers = []
    for c in base:
        customers.append({
            "customer_id": c["id"],
            "full_name": f"  {c['name']} " if rng.random() < 0.1 else c["name"],
            "email": c["email"],
            "phone": fmt_phone(rng, c["phone"]) if rng.random() < 0.9 else "",
            "country": rng.choice(COUNTRY_VARIANTS[c["country"]]),
            "signup_date": fmt_date(rng, c["signup"]),
        })

    # duplicates: same person re-entered under a new id, messy e-mail case
    dup_ids = {}
    for i, src in enumerate(rng.sample(base, 15)):
        new_id = 201 + i
        dup_ids[new_id] = src["id"]
        customers.append({
            "customer_id": new_id,
            "full_name": src["name"].upper(),
            "email": f" {src['email'].upper()} ",
            "phone": "",
            "country": "",
            "signup_date": fmt_date(rng, src["signup"] + timedelta(days=rng.randint(1, 60))),
        })

    # missing e-mail, invalid e-mail, bad dates, missing name, repeated id
    rejected_customer_ids = []
    for i in range(8):
        cid = 301 + i
        rejected_customer_ids.append(cid)
        customers.append({"customer_id": cid, "full_name": f"No Email {i}", "email": "",
                          "phone": "", "country": "Spain", "signup_date": "2024-01-15"})
    for i, bad in enumerate(["john.doe@", "no-at-sign.com", "a@b", "x y@example.com",
                             "@example.com", "plain"]):
        cid = 320 + i
        rejected_customer_ids.append(cid)
        customers.append({"customer_id": cid, "full_name": f"Bad Email {i}", "email": bad,
                          "phone": "", "country": "France", "signup_date": "2024-02-20"})
    for i, bad in enumerate(["31.02.2023", "n/a"]):
        cid = 330 + i
        customers.append({"customer_id": cid, "full_name": f"Bad Date {i}",
                          "email": f"bad.date.{i}@example.com", "phone": "",
                          "country": "Poland", "signup_date": bad})
    for i in range(3):
        customers.append({"customer_id": 340 + i, "full_name": "   ",
                          "email": f"no.name.{i}@example.com", "phone": "",
                          "country": "Germany", "signup_date": "2023-06-01"})
    # Rows stay in this order on purpose: the first row for a person wins,
    # so originals come before their re-entered duplicates.
    customers.append({"customer_id": 5, "full_name": "Id Clash",
                      "email": "id.clash@example.com", "phone": "",
                      "country": "UK", "signup_date": "2023-07-07"})

    # ---- orders -------------------------------------------------------
    orders = []
    next_id = 1

    def add(customer_id, order_date, amount, status, order_id=None):
        nonlocal next_id
        if order_id is None:
            order_id = next_id
            next_id += 1
        orders.append({"order_id": order_id, "customer_id": customer_id,
                       "order_date": order_date, "amount": amount, "status": status})

    def rand_date():
        return today - timedelta(days=rng.randint(0, 380))

    def rand_status():
        return rng.choice(rng.choice(list(STATUS_VARIANTS.values())))

    for _ in range(520):                                   # valid orders
        add(rng.choice(base)["id"], fmt_date(rng, rand_date()),
            fmt_amount(rng, rng.randint(500, 250000)), rand_status())
    for _ in range(20):                                    # via duplicate ids
        add(rng.choice(list(dup_ids)), fmt_date(rng, rand_date()),
            fmt_amount(rng, rng.randint(500, 90000)), rand_status())
    for _ in range(12):                                    # orphans
        add(9000 + rng.randint(1, 99), fmt_date(rng, rand_date()),
            fmt_amount(rng, 1999), "paid")
    for _ in range(5):                                     # rejected customers
        add(rng.choice(rejected_customer_ids), fmt_date(rng, rand_date()),
            fmt_amount(rng, 4500), "paid")
    for amount in ["-25.00", "(40.00)", "0", "0.00", "-1,200.00", "-5", "-9.99", "(1.00)"]:
        add(rng.choice(base)["id"], fmt_date(rng, rand_date()), amount, "refunded")
    for amount in ["N/A", "", "free", "TBD", "--", "n/a"]:
        add(rng.choice(base)["id"], fmt_date(rng, rand_date()), amount, "pending")
    for _ in range(4):                                     # bad status
        add(rng.choice(base)["id"], fmt_date(rng, rand_date()),
            fmt_amount(rng, 3000), rng.choice(["shipped?", "???", "", "lost"]))
    for _ in range(3):                                     # future dates
        add(rng.choice(base)["id"], "2099-01-01", fmt_amount(rng, 3000), "pending")
    for bad in ["2023-13-45", "yesterday", "", "99.99.9999"]:
        add(rng.choice(base)["id"], bad, fmt_amount(rng, 3000), "paid")
    for row in rng.sample(orders[:520], 10):               # exact duplicate rows
        add(row["customer_id"], row["order_date"], row["amount"], row["status"],
            order_id=row["order_id"])
    rng.shuffle(orders)

    # ---- write --------------------------------------------------------
    with open(out_dir / "customers_dirty.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(customers[0]))
        writer.writeheader()
        writer.writerows(customers)
    with open(out_dir / "orders_dirty.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(orders[0]))
        writer.writeheader()
        writer.writerows(orders)
    return {"customers": len(customers), "orders": len(orders)}


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "data"
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 42
    counts = generate(target, seed)
    print(f"Wrote {counts['customers']} customer rows and "
          f"{counts['orders']} order rows to {target}")
