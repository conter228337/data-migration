-- Sample checks and reports to run against output/clean.db
-- (sqlite3 output/clean.db < queries.sql)

.headers on
.mode column

-- 1. Revenue by country (paid orders only), in currency units
SELECT c.country,
       COUNT(*)                          AS orders,
       ROUND(SUM(o.amount_cents) / 100.0, 2) AS revenue
FROM orders o
JOIN customers c ON c.customer_id = o.customer_id
WHERE o.status = 'paid'
GROUP BY c.country
ORDER BY revenue DESC;

-- 2. Why rows were rejected
SELECT source, reason, COUNT(*) AS rows
FROM rejected_rows
GROUP BY source, reason
ORDER BY source, rows DESC;

-- 3. Customers that were merged from duplicates
SELECT a.source_customer_id AS merged_id,
       a.customer_id        AS kept_id,
       c.email
FROM customer_aliases a
JOIN customers c ON c.customer_id = a.customer_id
WHERE a.source_customer_id != a.customer_id
ORDER BY kept_id;

-- 4. Integrity check: should return no rows
SELECT o.order_id
FROM orders o
LEFT JOIN customers c ON c.customer_id = o.customer_id
WHERE c.customer_id IS NULL;
