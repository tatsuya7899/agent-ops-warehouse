-- Time series over kpi_snapshots, with a derived achievement rate so the
-- trend (not just the raw counters) is queryable directly.
--
-- ship_status / cadence_status: RAG signals for the two KPI-S/KPI-R
-- thresholds that CAREER-KPI states as fixed binary rules (ships >=4/month,
-- zero publications in the trailing two weeks = cadence violation). This is
-- the single place those thresholds are encoded so Looker Studio never
-- duplicates the pass/fail logic (SSOT — see SPEC "P2-2").
--
-- NULL input (e.g. a partial load) yields 'unknown', not a silent 'red' --
-- IF() would have collapsed a missing measurement into a false failure.
--
-- KPI-C (evidence_done/target/ratio) has no status column here on purpose:
-- its real threshold is a residual pace (remaining items / months to the
-- nearest gate deadline), which needs per-gate item counts and deadlines
-- this warehouse does not currently track. A fixed-ratio threshold would
-- misrepresent that policy, so it stays numbers-only until the schema
-- carries gate-level data.
select
    snapshot_date,
    kpi_c_achieved,
    kpi_c_total,
    safe_divide(kpi_c_achieved, kpi_c_total) as kpi_c_rate,
    streak_weeks,
    recent_two_week_pubs,
    case
        when recent_two_week_pubs is null then 'unknown'
        when recent_two_week_pubs > 0 then 'green'
        else 'red'
    end as cadence_status,
    evidence_ships,
    monthly_ships,
    case
        when monthly_ships is null then 'unknown'
        when monthly_ships >= 4 then 'green'
        else 'red'
    end as ship_status,
    loaded_at
from {{ ref('stg_kpi_snapshots') }}
order by snapshot_date
