-- Monthly activity across the whole studio: commits, active repos, articles.
-- The July 2026 row is the story: output volume concentrated the month the
-- governance system matured.
WITH monthly_commits AS (
  SELECT FORMAT_TIMESTAMP('%Y-%m', committed_at) AS month,
         COUNT(DISTINCT commit_hash) AS commits,
         COUNT(DISTINCT repo)        AS active_repos
  FROM `raw.git_commits`
  GROUP BY month
),
monthly_articles AS (
  SELECT FORMAT_DATE('%Y-%m', published_date) AS month,
         COUNT(*) AS articles
  FROM `raw.articles`
  GROUP BY month
)
SELECT c.month, c.commits, c.active_repos, COALESCE(a.articles, 0) AS articles
FROM monthly_commits c
LEFT JOIN monthly_articles a USING (month)
ORDER BY month DESC;
