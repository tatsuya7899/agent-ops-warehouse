-- Typed, trimmed view over raw.load_runs (the load ledger, SPEC section 3.2).
select
    cast(run_at as timestamp) as run_at,
    cast(source as string) as source,
    cast(rows_loaded as int64) as rows_loaded,
    trim(cast(exclusions_note as string)) as exclusions_note
from {{ source('raw', 'load_runs') }}
