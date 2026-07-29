-- Typed, trimmed view over raw.lessons.
-- Both active and archived rows pass through -- the 30-day forced-graduation
-- denominator needs both (SPEC section 3.1).
select
    cast(lesson_id as string) as lesson_id,
    cast(created_date as date) as created_date,
    cast(seq as int64) as seq,
    trim(cast(title as string)) as title,
    cast(status as string) as status,
    cast(graduated_to as string) as graduated_to,
    cast(loaded_at as timestamp) as loaded_at
from {{ source('raw', 'lessons') }}
