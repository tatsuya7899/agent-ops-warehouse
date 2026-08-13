variable "project_id" {
  description = "GCP project that hosts the warehouse. Set in terraform.tfvars (gitignored)."
  type        = string
}

variable "bq_location" {
  description = "BigQuery location. US multi-region keeps the free tier and Looker Studio compatibility."
  type        = string
  default     = "US"
}

# Referenced from P3 (Cloud Run). Declared now to pin the free-tier region
# decision (us-central1) made in the adversarial review — regions are the kind
# of default you get wrong silently.
# tflint-ignore: terraform_unused_declarations
variable "run_region" {
  description = "Cloud Run / GCS region. us-central1 is inside the Always Free tier."
  type        = string
  default     = "us-central1"
}

# P3 (Cloud Run, cloud_run.tf). Empty string by default -- and cloud_run.tf's
# resources are all `count`-gated on this being non-empty (2026-08-13, clean-
# fork reproduction test: a bare `terraform apply` on a fresh project failed
# immediately on this variable having no default at all, which blocked even
# the BigQuery-only Quickstart in README.md). The RAG API is an opt-in P3
# add-on, not part of the base warehouse; forcing every apply to supply an
# image path -- one that cannot exist until this same repo's Dockerfile has
# already been built and pushed once -- broke the "terraform apply ... in
# minutes" fork-and-deploy promise for anyone who only wants the warehouse.
# An operator who wants the RAG API sets this explicitly (-var or
# terraform.tfvars) once the image has been built and pushed to the
# google_artifact_registry_repository.rag_api repo created in cloud_run.tf,
# e.g.:
#   us-central1-docker.pkg.dev/<project_id>/rag-api/api:<tag>
variable "rag_api_image" {
  description = "Full Artifact Registry image path for the rag-api Cloud Run service (built and pushed manually from the repo-root Dockerfile). Format: <region>-docker.pkg.dev/<project_id>/rag-api/<image>:<tag>. Empty string (default) skips deploying the RAG API entirely -- it is an opt-in P3 add-on, not part of the base warehouse."
  type        = string
  default     = ""
}
