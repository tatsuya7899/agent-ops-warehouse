-- v1 scope-down (SPEC section 3.3): no draft-start timestamp is tracked
-- anywhere in the sources, so a true write-to-publish lead time cannot be
-- computed. This measures the gap between each article and the one
-- published immediately before it -- a cadence proxy for lead time --
-- aggregated to the month of publication.
with ordered as (
    select
        filename,
        published_date,
        lag(published_date) over (order by published_date) as previous_published_date
    from {{ ref('stg_articles') }}
),

gaps as (
    select
        filename,
        published_date,
        date_diff(published_date, previous_published_date, day) as days_since_previous_publish
    from ordered
)

select
    date_trunc(published_date, month) as month,
    count(*) as articles_published,
    avg(days_since_previous_publish) as avg_leadtime_proxy_days
from gaps
group by month
order by month
