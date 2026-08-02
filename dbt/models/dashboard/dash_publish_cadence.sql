-- Monthly publish cadence: article count and the average gap since the
-- previous publish, rounded for a clean number tile. See
-- mart_content_leadtime for the v1 lead-time-proxy caveat this inherits
-- (no draft-start timestamp exists, so this is a cadence proxy, not true
-- write-to-publish lead time).
select
    month,
    articles_published,
    round(avg_leadtime_proxy_days, 1) as avg_gap_days
from {{ ref('mart_content_leadtime') }}
order by month
