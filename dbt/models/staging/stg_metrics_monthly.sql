-- Typed, trimmed view over raw.metrics_monthly.
select
    cast(month as date) as month,
    cast(note_articles as int64) as note_articles,
    cast(note_views as int64) as note_views,
    cast(note_likes as int64) as note_likes,
    cast(note_comments as int64) as note_comments,
    cast(x_posts as int64) as x_posts,
    cast(x_impressions as int64) as x_impressions,
    cast(x_followers_total as int64) as x_followers_total,
    trim(cast(note_text as string)) as note_text,
    cast(loaded_at as timestamp) as loaded_at
from {{ source('raw', 'metrics_monthly') }}
