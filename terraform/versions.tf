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
  #
  # Deliberately a *partial* backend config (no bucket/prefix here) --
  # 2026-08-13 clean-fork reproduction test found that a hardcoded bucket
  # name here (this author's own private bucket, public access blocked)
  # made `terraform init` fail immediately for anyone else, before a fork
  # even reached the README's Quickstart apply step. Each user supplies
  # their own bucket/prefix via `-backend-config=backend.hcl` (gitignored;
  # see backend.hcl.example) or `-backend-config` flags, or omits it
  # entirely for local state (fine for trying this out; not recommended for
  # ongoing use -- see P2-3 in CHECKLIST for why local state was migrated
  # away from originally).
  backend "gcs" {}
}
