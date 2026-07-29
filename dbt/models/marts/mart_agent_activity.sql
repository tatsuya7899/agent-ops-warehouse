-- Weekly rollup of session_stats. Week starts Monday to line up with the
-- weekly delivery-streak instrument (CLAUDE.md governance: streak counted
-- weekly).
select
    date_trunc(stat_date, week(monday)) as week_start,
    sum(session_count) as session_count,
    sum(user_messages) as user_messages,
    sum(assistant_messages) as assistant_messages,
    sum(tool_calls) as tool_calls,
    max(distinct_tools) as max_distinct_tools
from {{ ref('stg_session_stats') }}
group by week_start
order by week_start
