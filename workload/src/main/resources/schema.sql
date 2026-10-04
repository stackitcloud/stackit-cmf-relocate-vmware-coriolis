CREATE TABLE IF NOT EXISTS migration_records (
    record_id text PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('seed', 'write')),
    payload text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO migration_records(record_id, kind, payload)
SELECT 'seed-' || lpad(n::text, 6, '0'), 'seed', md5('scf-relocate-' || n)
FROM generate_series(1, 1000) AS n
ON CONFLICT (record_id) DO NOTHING;
