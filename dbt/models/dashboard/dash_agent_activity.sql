-- Weekly session count only, chart-ready for a single bar/line series.
select
    week_start,
    session_count as sessions
from {{ ref('mart_agent_activity') }}
order by week_start
