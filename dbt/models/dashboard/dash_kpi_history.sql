-- Time-series companion to dash_kpi_current: every snapshot, same renamed
-- columns, for trend-line charts. loaded_at is dropped (pipeline metadata,
-- not a KPI).
select
    snapshot_date,
    streak_weeks,
    kpi_c_achieved as evidence_done,
    kpi_c_total as evidence_target,
    kpi_c_rate as evidence_ratio,
    recent_two_week_pubs as publications_last_two_weeks,
    monthly_ships as ships_this_month
from {{ ref('mart_kpi_history') }}
order by snapshot_date
