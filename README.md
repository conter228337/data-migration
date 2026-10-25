# CSV → SQL data migration demo

A small, readable pipeline that takes messy CSV exports (customers and orders), cleans them with explicit rules, and loads them into a relational database. Nothing is dropped silently: every rejected row is stored with the reason.

The sample data is **synthetic** (random fictional names, `example.com` e-mails) and deliberately dirty, so you can see each rule at work.

## What it handles

| Problem in the source | What the pipeline does |
|---|---|
| Same person entered twice (different id, e-mail in other case / extra spaces) | Merge by normalised e-mail, keep the earliest signup, fill missing phone/country from the duplicate, record the old id in `customer_aliases` |
| Orders pointing at a duplicate customer id | Re-pointed to the surviving customer |
| Mixed date formats (`2024-03-05`, `05.03.2024`, `03/05/2024`, `05 Mar 2024`) | Parsed to ISO dates; impossible dates (`31.02.2023`) rejected |
| Mixed money formats (`$1,234.50`, `1.234,50 EUR`, `12,5`, `(40.00)`) | Parsed to **integer cents**; unreadable, zero or negative amounts rejected |
| Missing / malformed e-mail, missing name | Rejected with a reason |
| Orders for unknown or rejected customers | Rejected as `unknown_customer` |
| Repeated order rows | First valid copy kept, the rest rejected as `duplicate_order_id` |
| Status spelling (`PAID`, `canceled`, `complete`) | Normalised to `paid / pending / refunded / cancelled` |
| Future dates | Rejected (relative to `--as-of`, default today) |
| Phone / country formatting | Digits-only phone (invalid ones set to NULL, row kept); country aliases mapped (`usa`, `deutschland`, …) |

After loading, the report checks that **rows read = loaded + merged + rejected** for both files.

## Run it

```bash
pip install -r requirements.txt          # only pytest, for the tests
python generate_dirty_data.py            # writes data/*.csv (seed 42, reproducible)
python migrate.py --customers data/customers_dirty.csv \
                  --orders data/orders_dirty.csv --as-of 2026-01-01
python -m pytest -q
```

Outputs: `output/clean.db` (SQLite), `output/rejects.csv`, and a text report on screen. The pipeline uses only the Python standard library (3.10+).

`queries.sql` has example reports (revenue by country, reject reasons, merged duplicates, integrity check). Run them with the `sqlite3` CLI or from Python.

## Layout

```
schema.sql              target tables, constraints, indexes
migrate.py              cleaning rules (pure functions) + loading + report
generate_dirty_data.py  reproducible messy sample data
tests/test_migrate.py   unit tests per rule + an end-to-end run
queries.sql             example reports and checks
```

## Design notes and assumptions

- **Money as integer cents** to avoid floating-point errors.
- **Slash dates are read as MM/DD/YYYY, dotted dates as DD.MM.YYYY.** This is an assumption; if your source differs, change `DATE_FORMATS` in `migrate.py`. Genuinely ambiguous data should be confirmed with the data owner.
- **E-mail is the identity key** for de-duplication. Customers without a valid e-mail are rejected, not guessed.
- Loading runs in **one transaction**, so a failure leaves no half-loaded database.
- The schema is plain SQL but was only run on **SQLite**. A PostgreSQL port should be straightforward but has not been tested.
