-- Typed, trimmed view over raw.session_stats.
-- Already aggregate counts only -- no transcript content in this layer.
select
    cast(stat_date as date) as stat_date,
    cast(session_count as int64) as session_count,
    cast(user_messages as int64) as user_messages,
    cast(assistant_messages as int64) as assistant_messages,
    cast(tool_calls as int64) as tool_calls,
    cast(distinct_tools as int64) as distinct_tools,
    cast(loaded_at as timestamp) as loaded_at
from {{ source('raw', 'session_stats') }}
