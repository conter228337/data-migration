-- Target schema for the cleaned data.
-- Written in plain SQL and run against SQLite by migrate.py.
-- (Moving it to PostgreSQL would need small changes, e.g. the date type
-- handling; that port has not been tested here.)

CREATE TABLE customers (
    customer_id  INTEGER PRIMARY KEY,
    full_name    TEXT    NOT NULL,
    email        TEXT    NOT NULL UNIQUE,
    phone        TEXT,
    country      TEXT,
    signup_date  DATE    NOT NULL
);

-- Records which source ids were merged into which canonical customer,
-- so duplicates stay traceable after the merge.
CREATE TABLE customer_aliases (
    source_customer_id  INTEGER PRIMARY KEY,
    customer_id         INTEGER NOT NULL REFERENCES customers (customer_id)
);

CREATE TABLE orders (
    order_id      INTEGER PRIMARY KEY,
    customer_id   INTEGER NOT NULL REFERENCES customers (customer_id),
    order_date    DATE    NOT NULL,
    amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
    status        TEXT    NOT NULL
                  CHECK (status IN ('paid', 'pending', 'refunded', 'cancelled'))
);

CREATE INDEX idx_orders_customer ON orders (customer_id);
CREATE INDEX idx_orders_date     ON orders (order_date);

-- Every row that was not loaded, with the reason, for review.
CREATE TABLE rejected_rows (
    source      TEXT    NOT NULL,   -- 'customers' or 'orders'
    source_row  INTEGER NOT NULL,   -- line number in the CSV (header = 1)
    reason      TEXT    NOT NULL,
    raw_data    TEXT    NOT NULL
);
