-- Typed, trimmed view over raw.git_commits.
-- subject is passed through untouched here (never rendered raw on public
-- surfaces -- that rule applies to consumers, not to this internal layer).
select
    cast(repo as string) as repo,
    cast(commit_hash as string) as commit_hash,
    cast(committed_at as timestamp) as committed_at,
    trim(cast(subject as string)) as subject,
    cast(files_changed as int64) as files_changed,
    cast(insertions as int64) as insertions,
    cast(deletions as int64) as deletions,
    cast(loaded_at as timestamp) as loaded_at
from {{ source('raw', 'git_commits') }}
