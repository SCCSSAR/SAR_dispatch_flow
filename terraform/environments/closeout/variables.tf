variable "project_id" {
  description = "GCP project ID for the close-out helper's Vertex project. Must match the backend bucket name in main.tf."
  type        = string
  default     = "sar-closeout-sccssar"
}

variable "region" {
  description = "Default region for the provider. Gemini calls themselves use the 'us' multi-region endpoint."
  type        = string
  default     = "us-central1"
}

variable "alert_notification_email" {
  description = "Email address for CIS security alert notifications"
  type        = string
  # Set in terraform.tfvars — no default, this is security-sensitive
}

variable "closeout_users" {
  description = "People allowed to run the helper, as IAM principals: \"user:<name>@sccssar.org\" or \"group:<group>@sccssar.org\""
  type        = list(string)

  validation {
    condition     = alltrue([for m in var.closeout_users : can(regex("^(user|group):[^@\\s]+@sccssar\\.org$", m))])
    error_message = "Each entry must be a user: or group: principal at sccssar.org. Service accounts, domains, allUsers and allAuthenticatedUsers are not allowed."
  }
}
