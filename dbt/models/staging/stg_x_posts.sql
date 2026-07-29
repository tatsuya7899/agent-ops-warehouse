-- Typed, trimmed view over raw.x_posts.
select
    cast(posted_at as timestamp) as posted_at,
    cast(post_type as string) as post_type,
    trim(cast(theme as string)) as theme,
    cast(url as string) as url,
    cast(char_count as int64) as char_count,
    cast(loaded_at as timestamp) as loaded_at
from {{ source('raw', 'x_posts') }}
