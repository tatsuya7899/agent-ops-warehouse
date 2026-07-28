variable "project_id" {
  description = "GCP project that hosts the warehouse. Set in terraform.tfvars (gitignored)."
  type        = string
}

variable "bq_location" {
  description = "BigQuery location. US multi-region keeps the free tier and Looker Studio compatibility."
  type        = string
  default     = "US"
}

variable "run_region" {
  description = "Cloud Run / GCS region. us-central1 is inside the Always Free tier."
  type        = string
  default     = "us-central1"
}
