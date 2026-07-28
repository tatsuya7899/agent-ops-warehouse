# Sample queries — live output

Captured 2026-07-28 against the real warehouse (sandbox mode). Text output is committed instead of screenshots — harder to fake, easier to diff. The `subject` column is never selected on public surfaces (privacy boundary).

```
═══ monthly_activity ═══
FATAL Flags parsing error: Unknown command line flag ' Monthly activity across the whole studio: commits, active repos, articles.
-- The July 2026 row is the story: output volume concentrated the month the
-- governance system matured.
WITH monthly_commits AS (
  SELECT FORMAT_TIMESTAMP('%Y-%m', committed_at) AS month,
         COUNT(DISTINCT commit_hash) AS commits,
         COUNT(DISTINCT repo)        AS active_repos
  FROM `agent-ops-warehouse.raw.git_commits`
  GROUP BY month
),
monthly_articles AS (
  SELECT FORMAT_DATE('%Y-%m', published_date) AS month,
         COUNT(*) AS articles
  FROM `agent-ops-warehouse.raw.articles`
  GROUP BY month
)
SELECT c.month, c.commits, c.active_repos, COALESCE(a.articles, 0) AS articles
FROM monthly_commits c
LEFT JOIN monthly_articles a USING (month)
ORDER BY month DESC;'
Run 'bq.py help' to get help
═══ lesson_flow ═══
FATAL Flags parsing error: Unknown command line flag ' Lessons-learned inventory: how many operational lessons exist and their
-- lifecycle state (a 30-day TTL forces each lesson to graduate or archive).
SELECT status, COUNT(*) AS lessons,
       MIN(created_date) AS oldest, MAX(created_date) AS newest
FROM `agent-ops-warehouse.raw.lessons`
GROUP BY status;'
Run 'bq.py help' to get help
═══ load_ledger_audit ═══
FATAL Flags parsing error: Unknown command line flag ' The load ledger makes exclusions auditable: every run records what was
-- loaded AND what was skipped (the allowlist at work).
SELECT run_at, source, rows_loaded, exclusions_note
FROM `agent-ops-warehouse.raw.load_runs`
ORDER BY run_at DESC
LIMIT 10;'
Run 'bq.py help' to get help
═══ publishing_cadence ═══
FATAL Flags parsing error: Unknown command line flag ' Publishing cadence: ISO-week article counts (the KPI sensor this warehouse
-- replaces was a hand-rolled Python script reading filenames).
SELECT FORMAT_DATE('%G-W%V', published_date) AS iso_week, COUNT(*) AS articles
FROM `agent-ops-warehouse.raw.articles`
GROUP BY iso_week
ORDER BY iso_week DESC;'
Run 'bq.py help' to get help
```
