terraform {
  required_version = ">= 1.7"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
  # P2: migrate state to a GCS backend (us-central1). Local state during P1
  # avoids the bootstrap circular dependency (see SPEC design decision 3).
}
