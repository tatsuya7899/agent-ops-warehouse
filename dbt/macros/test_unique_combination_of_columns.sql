{#
  Composite-key acceptance test, hand-rolled instead of pulling in dbt_utils
  (SPEC section 10: dbt_utils is not adopted -- this is the one test it would
  have provided that dbt-core lacks natively). Fails a row group whenever the
  given column combination is not unique, mirroring dbt-core's built-in
  `unique` test but for more than one column.

  Usage (schema.yml):
    tests:
      - unique_combination_of_columns:
          combination_of_columns: ['repo', 'commit_hash']
#}
{% test unique_combination_of_columns(model, combination_of_columns) %}

with validation as (

    select
        {{ combination_of_columns | join(', ') }},
        count(*) as num_rows

    from {{ model }}
    group by {{ combination_of_columns | join(', ') }}

),

validation_errors as (

    select *
    from validation
    where num_rows > 1

)

select *
from validation_errors

{% endtest %}
