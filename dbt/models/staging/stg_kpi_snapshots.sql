-- Typed, trimmed view over raw.kpi_snapshots.
select
    cast(snapshot_date as date) as snapshot_date,
    cast(kpi_c_achieved as int64) as kpi_c_achieved,
    cast(kpi_c_total as int64) as kpi_c_total,
    cast(streak_weeks as int64) as streak_weeks,
    cast(recent_two_week_pubs as int64) as recent_two_week_pubs,
    cast(evidence_ships as int64) as evidence_ships,
    cast(monthly_ships as int64) as monthly_ships,
    cast(loaded_at as timestamp) as loaded_at
from {{ source('raw', 'kpi_snapshots') }}
