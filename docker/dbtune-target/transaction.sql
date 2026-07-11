\set source_id random(1, 100000)
\set target_id random(1, 100000)
\set transfer random(1, 25)
BEGIN;
UPDATE accounts
SET balance = balance - :transfer, updated_at = NOW()
WHERE id = :source_id;
SELECT pg_sleep(0.015);
UPDATE accounts
SET balance = balance + :transfer, updated_at = NOW()
WHERE id = :target_id;
INSERT INTO ledger (account_id, amount)
VALUES (:source_id, -:transfer), (:target_id, :transfer);
COMMIT;
