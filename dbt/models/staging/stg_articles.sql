-- Typed, trimmed view over raw.articles.
select
    cast(filename as string) as filename,
    cast(published_date as date) as published_date,
    trim(cast(title as string)) as title,
    cast(gate_id as string) as gate_id,
    cast(char_count as int64) as char_count,
    cast(loaded_at as timestamp) as loaded_at
from {{ source('raw', 'articles') }}
