-- Chart-shaped copy of mart_ship_velocity: plain-noun column names so this
-- drops straight onto a Looker Studio time-series/combo chart with `month`
-- as the dimension, no calculated fields required.
select
    month,
    article_count as articles,
    commit_count as commits,
    x_post_count as x_posts,
    ship_velocity_total as total
from {{ ref('mart_ship_velocity') }}
order by month
