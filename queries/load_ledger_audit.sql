-- The load ledger makes exclusions auditable: every run records what was
-- loaded AND what was skipped (the allowlist at work).
SELECT run_at, source, rows_loaded, exclusions_note
FROM `raw.load_runs`
ORDER BY run_at DESC
LIMIT 10;
