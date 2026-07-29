{#
  Standard dbt override: without this, BigQuery datasets would be named
  "<target.dataset>_<custom_schema_name>" (e.g. "staging_staging"). Each
  model group here declares its own +schema (staging / marts) in
  dbt_project.yml, and that name should be used verbatim -- these datasets
  already exist as first-class layers (Terraform-managed, see
  terraform/main.tf), not as a dbt-managed suffix.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
