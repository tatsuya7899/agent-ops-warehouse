-- Time series over kpi_snapshots, with a derived achievement rate so the
-- trend (not just the raw counters) is queryable directly.
select
    snapshot_date,
    kpi_c_achieved,
    kpi_c_total,
    safe_divide(kpi_c_achieved, kpi_c_total) as kpi_c_rate,
    streak_weeks,
    recent_two_week_pubs,
    evidence_ships,
    monthly_ships,
    loaded_at
from {{ ref('stg_kpi_snapshots') }}
order by snapshot_date
