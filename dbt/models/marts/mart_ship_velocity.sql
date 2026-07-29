-- Monthly Ship count proxy: article count + commit count + X-post count.
-- One row per calendar month found in the union of all three sources'
-- active months (so a month with commits but no article/post still appears).
with article_months as (
    select date_trunc(published_date, month) as month
    from {{ ref('stg_articles') }}
),

commit_months as (
    select date_trunc(date(committed_at), month) as month
    from {{ ref('stg_git_commits') }}
),

x_post_months as (
    select date_trunc(date(posted_at), month) as month
    from {{ ref('stg_x_posts') }}
),

months as (
    select month from article_months
    union distinct
    select month from commit_months
    union distinct
    select month from x_post_months
),

articles_monthly as (
    select month, count(*) as article_count
    from article_months
    group by month
),

commits_monthly as (
    select month, count(*) as commit_count
    from commit_months
    group by month
),

x_posts_monthly as (
    select month, count(*) as x_post_count
    from x_post_months
    group by month
)

select
    months.month,
    coalesce(articles_monthly.article_count, 0) as article_count,
    coalesce(commits_monthly.commit_count, 0) as commit_count,
    coalesce(x_posts_monthly.x_post_count, 0) as x_post_count,
    coalesce(articles_monthly.article_count, 0)
        + coalesce(commits_monthly.commit_count, 0)
        + coalesce(x_posts_monthly.x_post_count, 0) as ship_velocity_total
from months
left join articles_monthly using (month)
left join commits_monthly using (month)
left join x_posts_monthly using (month)
order by months.month
