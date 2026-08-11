# agent-ops-warehouse — BigQuery layers managed as code.
#
# System of record stays on the local machine (git history, Markdown files).
# Everything here is derived data and can be rebuilt at any time by re-running
# the loader, which is why resources are recreated rather than imported.

provider "google" {
  project = var.project_id
}

locals {
  datasets = {
    raw     = "Raw layer: faithful replicas of local sources (system of record = local git/files)."
    staging = "Staging layer: typed and cleaned views over raw (managed by dbt)."
    marts   = "Marts layer: derived, recomputable views (managed by dbt)."
  }

  # Table schemas are versioned JSON files: additive column changes only.
  raw_tables = {
    git_commits     = "schemas/raw_git_commits.json"
    articles        = "schemas/raw_articles.json"
    x_posts         = "schemas/raw_x_posts.json"
    metrics_monthly = "schemas/raw_metrics_monthly.json"
    lessons         = "schemas/raw_lessons.json"
    session_stats   = "schemas/raw_session_stats.json"
    kpi_snapshots   = "schemas/raw_kpi_snapshots.json"
    load_runs       = "schemas/raw_load_runs.json"
    article_chunks  = "schemas/raw_article_chunks.json"
  }
}

resource "google_bigquery_dataset" "layers" {
  for_each    = local.datasets
  dataset_id  = each.key
  location    = var.bq_location
  description = each.value
  # No default_table_expiration_ms: tables must never silently expire.
  # (Sandbox-created datasets carry a 60-day default; recreation removes it.)
}

resource "google_bigquery_table" "raw" {
  for_each            = local.raw_tables
  dataset_id          = google_bigquery_dataset.layers["raw"].dataset_id
  table_id            = each.key
  schema              = file("${path.module}/${each.value}")
  deletion_protection = false # derived data; rebuilt by the loader at will
}

# P1: budget alert (JPY 1,000 ~= USD 5, thresholds 50/90/100%). Created via
# `gcloud billing budgets create` first (billing-account IAM friction with
# user ADC blocks Terraform here), imported later:
#   terraform import google_billing_budget.guard <budget-id>
