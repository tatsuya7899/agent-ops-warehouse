# P0 evidence — first aggregation query (2026-07-28)

Sandbox mode (no billing). Dataset `raw`, location US.
Loaded: git_commits=177 rows / articles=15 rows / load_runs=2 rows (batch load, free tier).

## Query

```sql
WITH monthly_commits AS (
  SELECT FORMAT_TIMESTAMP("%Y-%m", committed_at) AS month,
         COUNT(DISTINCT commit_hash) AS commits,
         COUNT(DISTINCT repo) AS active_repos
  FROM `agent-ops-warehouse.raw.git_commits`
  GROUP BY month
),
monthly_articles AS (
  SELECT FORMAT_DATE("%Y-%m", published_date) AS month,
         COUNT(*) AS articles_published
  FROM `agent-ops-warehouse.raw.articles`
  GROUP BY month
)
SELECT c.month, c.commits, c.active_repos,
       COALESCE(a.articles_published, 0) AS articles_published
FROM monthly_commits c
LEFT JOIN monthly_articles a USING (month)
ORDER BY month DESC
LIMIT 12
```

## Output

```
+---------+---------+--------------+--------------------+
|  month  | commits | active_repos | articles_published |
+---------+---------+--------------+--------------------+
| 2026-07 |     106 |            4 |                 15 |
| 2026-03 |      25 |            1 |                  0 |
| 2026-02 |       3 |            1 |                  0 |
| 2026-01 |      16 |            1 |                  0 |
| 2025-12 |       1 |            1 |                  0 |
| 2025-06 |       1 |            1 |                  0 |
| 2025-05 |      25 |            1 |                  0 |
+---------+---------+--------------+--------------------+
```

Note: sandbox datasets carry a default table expiration (60 days). Removal is a hard
completion criterion of P1 (`bq show` proof) after billing upgrade.
