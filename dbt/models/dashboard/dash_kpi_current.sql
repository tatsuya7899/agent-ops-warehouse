-- Scorecard tiles: the single latest KPI snapshot. Columns are renamed to
-- the evidence/streak vocabulary CLAUDE.md uses (evidence_* = KPI-C gate
-- evidence progress, streak_weeks = the weekly delivery-streak instrument)
-- so each one maps directly to a Looker Studio number tile.
select
    snapshot_date as as_of_date,
    streak_weeks,
    kpi_c_achieved as evidence_done,
    kpi_c_total as evidence_target,
    kpi_c_rate as evidence_ratio,
    recent_two_week_pubs as publications_last_two_weeks,
    monthly_ships as ships_this_month
from {{ ref('mart_kpi_history') }}
order by snapshot_date desc
limit 1
