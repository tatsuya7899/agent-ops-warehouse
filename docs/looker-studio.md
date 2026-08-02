# Looker Studio dashboard (P2) — build spec

One page, four charts, all reading the `marts` dataset. Looker Studio is free;
the marts are plain BigQuery views, so the dashboard adds zero cost and zero
infrastructure — it is configuration, not code, which is why this spec (not a
binary) is what lives in the repo.

## Setup (once, ~5 min)

1. Open lookerstudio.google.com → **Create → Report**.
2. Add data → **BigQuery** → project `agent-ops-warehouse` → dataset `marts`
   → add each of the four tables below as a data source.
3. Theme: default. Date fields: BigQuery types map automatically
   (`month` is a DATE truncated to month; set its granularity to
   "Year Month" in each chart).

## Charts (one per mart)

| # | Chart | Source | Config |
|---|---|---|---|
| 1 | **Ship velocity** — stacked column + line | `mart_ship_velocity` | Dimension `month` (Year Month). Stacked bars: `article_count`, `commit_count`, `x_post_count`. Optional line series: `ship_velocity_total`. This is the north-star proxy over time |
| 2 | **KPI history** — time series | `mart_kpi_history` | Dimension `snapshot_date`. Metrics: the KPI columns (streak / evidence counters). One snapshot per load, so expect a sparse line that densifies as weekly loads accumulate |
| 3 | **Agent activity** — column | `mart_agent_activity` | Dimension `week_start` (Year Week). Metric `session_count`. Weekly rollup — content-free by design (counts only, per the privacy boundary) |
| 4 | **Publishing cadence** — combo | `mart_content_leadtime` | Dimension `month` (Year Month). Bars: `articles_published`. Line: the average publish-gap column (cadence proxy — the README explains why true lead time is out of scope) |

## Sharing

Keep the report private by default. "Anyone with the link can view" is a
deliberate publish action — treat it like shipping (it goes through the same
review gate as any public artifact).

## Why the dashboard itself is not code

Looker Studio has no usable IaC surface on the free tier. The reproducible
part is everything upstream (Terraform + loader + dbt); the dashboard is a
15-minute manual assembly documented here, which a fork can follow verbatim.
