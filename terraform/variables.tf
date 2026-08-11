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

# P3 (Cloud Run, cloud_run.tf). No default on purpose: this image does not
# exist until an operator builds it from the repo-root Dockerfile and pushes
# it to the google_artifact_registry_repository.rag_api repo created in
# cloud_run.tf, e.g.:
#   us-central1-docker.pkg.dev/<project_id>/rag-api/api:<tag>
# A placeholder default would let `terraform apply` silently deploy a
# service pointing at an image that was never actually built.
variable "rag_api_image" {
  description = "Full Artifact Registry image path for the rag-api Cloud Run service (built and pushed manually from the repo-root Dockerfile). Format: <region>-docker.pkg.dev/<project_id>/rag-api/<image>:<tag>."
  type        = string
}
