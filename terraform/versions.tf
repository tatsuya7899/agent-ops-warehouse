terraform {
  required_version = ">= 1.7"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
  # P2-3 (2026-08-12): migrated to a GCS backend (us-central1, Always Free
  # tier per SPEC-agent-ops-warehouse.md §4 region pinning). Local state
  # during P1 avoided the bootstrap circular dependency (bucket itself would
  # need to be Terraform-managed before Terraform could use it as a backend).
  backend "gcs" {
    bucket = "agent-ops-warehouse-tfstate"
    prefix = "terraform/state"
  }
}
