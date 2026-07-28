output "dataset_ids" {
  value       = [for d in google_bigquery_dataset.layers : d.dataset_id]
  description = "Created BigQuery datasets."
}
