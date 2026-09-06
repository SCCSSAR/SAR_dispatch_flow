output "cloud_run_url" {
  description = "URL of the deployed Cloud Run backend service"
  value       = google_cloud_run_v2_service.dispatch.uri
}

output "service_account_email" {
  description = "Email of the Cloud Run service account"
  value       = google_service_account.dispatch_runner.email
  sensitive   = true
}

output "artifact_registry_repo" {
  description = "Artifact Registry repository URL for Docker images"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/dispatch-console"
  sensitive   = true
}

output "docker_image_path" {
  description = "Full Docker image path to use in builds and deploys"
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/dispatch-console/dispatch-console:latest"
  sensitive   = true
}
