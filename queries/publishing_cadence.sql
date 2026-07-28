-- Publishing cadence: ISO-week article counts (the KPI sensor this warehouse
-- replaces was a hand-rolled Python script reading filenames).
SELECT FORMAT_DATE('%G-W%V', published_date) AS iso_week, COUNT(*) AS articles
FROM `raw.articles`
GROUP BY iso_week
ORDER BY iso_week DESC;
