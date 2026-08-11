# Cloud Run RAG API (SPEC-agent-ops-warehouse-rag-api_20260811.md Section
# 4.5 / Section 9 phase 4). Split out of main.tf: main.tf manages only
# BigQuery datasets/tables (the P1 pattern this project already had), and
# service accounts / IAM / Secret Manager / Artifact Registry / Cloud Run
# are, per the SPEC's own note, "このプロジェクトで前例のない新規カテゴ
# リ" -- kept in their own file for readability rather than folded into
# main.tf's existing BigQuery-only shape.

# ---------------------------------------------------------------------------
# Service account: a dedicated runtime identity for the Cloud Run service,
# not the Terraform-applying user's own credentials and not the project's
# default compute service account (SPEC Section 4.5: "Cloud Run専用の新規
# サービスアカウントを作成し...最小権限のみ付与する").
# ---------------------------------------------------------------------------

resource "google_service_account" "rag_api" {
  account_id   = "rag-api"
  display_name = "RAG API (Cloud Run)"
  # GCP caps service account descriptions at 256 chars -- the fuller
  # rationale (SPEC Section 4.5: least-privilege scope) lives in this
  # file's comments above/below instead of in the resource itself.
  description = "Runtime identity for the rag-api Cloud Run service. Least privilege: BigQuery raw-dataset read + job execution, Secret Manager read of API_TOKEN/GEMINI_API_KEY only."
}

# ---------------------------------------------------------------------------
# IAM: least privilege, deliberately scoped narrower than the SPEC's own
# prose ("project全体" in Section 4.5) where the underlying GCP IAM binding
# type allows it. bigquery.dataViewer and secretmanager.secretAccessor
# both support binding at the resource they actually protect (a dataset, a
# secret) instead of the whole project; bigquery.jobUser does not -- BigQuery
# job execution is a project-level permission in GCP's own IAM model, so
# that one binding is legitimately project-scoped, not a shortcut taken here.
# ---------------------------------------------------------------------------

# roles/bigquery.dataViewer, scoped to the `raw` dataset only (article_chunks
# lives there -- see main.tf's google_bigquery_table.raw). Not project-wide:
# this identity has no read access to any other dataset (staging/marts).
resource "google_bigquery_dataset_iam_member" "rag_api_data_viewer" {
  dataset_id = google_bigquery_dataset.layers["raw"].dataset_id
  role       = "roles/bigquery.dataViewer"
  member     = "serviceAccount:${google_service_account.rag_api.email}"
}

# roles/bigquery.jobUser: running a BigQuery query job (VECTOR_SEARCH, SPEC
# Section 4.3) is a project-level permission in GCP's IAM model -- there is
# no per-dataset "run a job" grant to narrow this to. Project-scoped here is
# the correct minimum, not an unscoped default.
resource "google_project_iam_member" "rag_api_bigquery_job_user" {
  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.rag_api.email}"
}

# roles/secretmanager.secretAccessor, scoped per-secret (not project-wide):
# this identity can read API_TOKEN and GEMINI_API_KEY specifically, and
# nothing else that might later live in this project's Secret Manager.
resource "google_secret_manager_secret_iam_member" "api_token_accessor" {
  secret_id = google_secret_manager_secret.api_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.rag_api.email}"
}

resource "google_secret_manager_secret_iam_member" "gemini_api_key_accessor" {
  secret_id = google_secret_manager_secret.gemini_api_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.rag_api.email}"
}

# ---------------------------------------------------------------------------
# Secret Manager: containers only. Values (secret versions) are deliberately
# NOT written here -- SPEC Section 4.5: "Terraformにハードコードしない・
# 既存の.env運用を踏襲". The operator injects them out of band with
# `gcloud secrets versions add API_TOKEN --data-file=-` (and the same for
# GEMINI_API_KEY) after `terraform apply`.
# ---------------------------------------------------------------------------

resource "google_secret_manager_secret" "api_token" {
  secret_id = "API_TOKEN"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "gemini_api_key" {
  secret_id = "GEMINI_API_KEY"
  replication {
    auto {}
  }
}

# ---------------------------------------------------------------------------
# Artifact Registry: where the image built from this repo's root Dockerfile
# is pushed before a Cloud Run revision can reference it.
# ---------------------------------------------------------------------------

resource "google_artifact_registry_repository" "rag_api" {
  repository_id = "rag-api"
  location      = var.run_region
  format        = "DOCKER"
  description   = "Container images for the rag-api Cloud Run service (SPEC Section 4.5)."
}

# ---------------------------------------------------------------------------
# Cloud Run service.
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "rag_api" {
  name     = "rag-api"
  location = var.run_region

  template {
    service_account = google_service_account.rag_api.email

    # SPEC Section 4.5 / Section 8 risk table: max_instance_count=1 is not
    # merely a cost cap -- api/main.py's daily request-count limiter
    # (_daily_request_counts) is an in-memory, module-level dict, correct
    # ONLY under a single running instance. Raising this above 1 would
    # silently multiply the effective daily limit by instance count without
    # any error (see api/main.py's own comment on _daily_request_counts).
    # Do not change this value without revisiting that limiter.
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = var.rag_api_image

      ports {
        container_port = 8080
      }

      # Plain env vars (not secrets) -- SPEC Section 4.5's 2026-08-12
      # addendum: api/main.py reads GCP_PROJECT_ID as a required env var
      # (RetrievalError -> 503 on every /query request if unset) and
      # BQ_DATASET (defaults to "raw" in code, set explicitly here anyway
      # so the deployed configuration doesn't rely on that code default
      # silently matching).
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }

      env {
        name  = "BQ_DATASET"
        value = "raw"
      }

      # Secret-backed env vars -- SPEC Section 4.5: API_TOKEN and
      # GEMINI_API_KEY come from Secret Manager, never a plain `value`.
      env {
        name = "API_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.api_token.secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "GEMINI_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.gemini_api_key.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.api_token_accessor,
    google_secret_manager_secret_iam_member.gemini_api_key_accessor,
  ]
}

# ---------------------------------------------------------------------------
# Public invocation. Design decision (deliberately not GCP-IAM-gated):
# this API's real access control is application-layer Bearer-token auth
# (api/main.py's require_api_token, checked against the API_TOKEN secret
# above), not Cloud Run's own IAM invoker check -- and /health is
# explicitly unauthenticated by design (SPEC Section 4.4: "認証なし・課金
# にも繋がらない軽量応答"). Requiring roles/run.invoker on top of that would
# add a second, GCP-account-scoped authentication layer that:
#   (a) `/health` was never designed to satisfy (it would stop being a
#       plain unauthenticated liveness check), and
#   (b) scripts/query_articles.sh (SPEC Section 4.4b, the curl wrapper
#       CLI) has no GCP-credential-fetching step in its design -- it only
#       ever sends the Bearer token -- so a private Cloud Run service would
#       make that script's whole design (a bare `curl` call) unable to
#       reach the service at all.
# allUsers here means "reachable", not "unauthenticated" -- authentication
# still happens one layer up, inside the app.
resource "google_cloud_run_v2_service_iam_member" "rag_api_public_invoker" {
  name     = google_cloud_run_v2_service.rag_api.name
  location = google_cloud_run_v2_service.rag_api.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}
