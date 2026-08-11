-- Typed, trimmed view over raw.article_chunks.
select
    cast(chunk_id as string) as chunk_id,
    cast(filename as string) as filename,
    trim(cast(article_title as string)) as article_title,
    trim(cast(section_title as string)) as section_title,
    trim(cast(chunk_text as string)) as chunk_text,
    cast(embedding as array<float64>) as embedding,
    cast(published_date as date) as published_date,
    cast(loaded_at as timestamp) as loaded_at
from {{ source('raw', 'article_chunks') }}
