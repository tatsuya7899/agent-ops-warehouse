variable "project_id" {
  description = "GCP project that hosts the warehouse. Set in terraform.tfvars (gitignored)."
  type        = string
}

variable "bq_location" {
  description = "BigQuery location. US multi-region keeps the free tier and Looker Studio compatibility."
  type        = string
  default     = "US"
}

# tflint-ignore: terraform_unused_declarations
# Referenced from P3 (Cloud Run). Declared now to pin the free-tier region
# decision (us-central1) made in the adversarial review — regions are the kind
# of default you get wrong silently.
variable "run_region" {
  description = "Cloud Run / GCS region. us-central1 is inside the Always Free tier."
  type        = string
  default     = "us-central1"
}
