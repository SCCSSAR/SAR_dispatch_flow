variable "project_id" {
  description = "GCP project ID for the SCCSSAR dev environment"
  type        = string
  default     = "sar-dispatch-sccssar-dev"
}

variable "project_number" {
  description = "GCP project number for the SCCSSAR dev environment"
  type        = string
  default     = "1010784158087"
}

variable "region" {
  description = "GCP region for all resources"
  type        = string
  default     = "us-central1"
}

variable "authorized_dispatcher_emails" {
  description = "Comma-separated list of Google email addresses authorized to use the OCR endpoint"
  type        = string
  sensitive   = true
  # Set in terraform.tfvars (gitignored) — never commit this value
}

variable "google_client_id" {
  description = "OAuth2 client ID for Google Sign-In (from GCP Console → APIs & Services → Credentials)"
  type        = string
  sensitive   = true
  # Set in terraform.tfvars (gitignored)
}

variable "ocr_rate_limit_per_minute" {
  description = "Max OCR calls per user per minute"
  type        = number
  default     = 5
}

variable "ocr_rate_limit_per_hour" {
  description = "Max OCR calls per user per hour"
  type        = number
  default     = 20
}

variable "ocr_rate_limit_per_day" {
  description = "Max OCR calls per user per day"
  type        = number
  default     = 50
}

variable "ocr_daily_global_cap" {
  description = "Max OCR calls per day across all users (project-level hard ceiling)"
  type        = number
  default     = 200
}

variable "max_upload_size_mb" {
  description = "Maximum JPEG upload size in MB"
  type        = number
  default     = 10
}

variable "gemini_model" {
  description = "Vertex AI Gemini model ID to use for OCR"
  type        = string
  default     = "gemini-2.5-flash"
}

variable "log_level" {
  description = "Python logging level for the backend"
  type        = string
  # INFO is the default for ALL environments to suppress third-party-library
  # DEBUG output (httpx request URLs, google.auth token refresh, urllib3
  # connection details) which can leak request-path data into Cloud Run logs.
  # Backend code's own logger.debug calls are skip/graceful-degrade messages
  # (10 sites — none load-bearing). When actively debugging on sccssar-dev,
  # set `log_level = "DEBUG"` in the gitignored terraform.tfvars and re-apply.
  default     = "INFO"
}

variable "allowed_origins" {
  description = "Comma-separated list of allowed CORS origins (frontend URL)"
  type        = string
  default     = "https://dispatch-console-1010784158087.us-central1.run.app"
  # Cloud Run URL — both project-number format and hash-based format are valid;
  # gcloud run deploy reports the project-number format as the canonical Service URL.
  # Override in terraform.tfvars if URL ever changes.
}

variable "cloud_run_service_name" {
  description = "Name of the Cloud Run service"
  type        = string
  default     = "dispatch-console"
}

# ---------------------------------------------------------------------------
# Everbridge + Slack integration (Phase 5 SCCSSAR-dev mirror — PR-1)
#
# DESIGN DECISION (do not revert without team discussion):
# Both `everbridge_mode` and `slack_mode` default to "" (off) here.
# This is the OPPOSITE of the personal-dev pattern (where EVERBRIDGE_MODE is
# hardcoded "safe" in main.tf as a safety net). SCCSSAR-dev is the controlled
# rollout target — the staged Phase 5 PR sequence flips these via terraform.tfvars:
#   PR-4 (solo live test):       everbridge_mode = "safe",  slack_mode = "shadow"
#   PR-5 (multi-recipient test): everbridge_mode = "safe",  slack_mode = "shadow"
#   PR-6A (EB live, mid-May):    everbridge_mode = "full",  slack_mode = "shadow"
#   PR-6B (Slack live, mid-July): everbridge_mode = "full",  slack_mode = "full"
# ---------------------------------------------------------------------------

variable "everbridge_mode" {
  description = "Everbridge dispatch mode: '' (feature off / hidden), 'safe' (sends only to safe-list contacts), or 'full' (unconditional live sends)."
  type        = string
  default     = ""
  validation {
    condition     = contains(["", "safe", "full"], var.everbridge_mode)
    error_message = "everbridge_mode must be '', 'safe', or 'full'."
  }
}

variable "slack_mode" {
  description = "Slack auto-invite mode: '' (feature off / hidden), 'shadow' (channels created, only safe-list invited; per-cycle would-invite messages), or 'full' (per-responder invites enabled)."
  type        = string
  default     = ""
  validation {
    condition     = contains(["", "shadow", "full"], var.slack_mode)
    error_message = "slack_mode must be '', 'shadow', or 'full'."
  }
}

variable "active_incidents_channel_id" {
  description = "Slack channel ID for #active-incidents (private; live tally posted here). Same workspace as personal-dev — channel ID is reused. Empty default — must be set in terraform.tfvars before flipping slack_mode away from ''."
  type        = string
  default     = ""
}

variable "active_incident_mgmt_usergroup_id" {
  description = "Slack user-group ID for @active_incident_management (issue #579). Admin-curated members pre-added to every incident channel, replacing #active-incidents membership as the pre-add source. Same workspace as personal-dev — group ID is reused. Empty default — helper degrades to dispatcher + SO coordinator (best-effort) when unset."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Aikido CIS Log-based Metrics + Alert Policies (Phase 5 SCCSSAR-dev mirror — PR-2)
# ---------------------------------------------------------------------------

variable "alert_notification_email" {
  description = "Email address for CIS security alert notifications (project ownership, audit config, firewall rule changes)"
  type        = string
  # Set in terraform.tfvars — no default, this is security-sensitive
}

variable "everbridge_caller_id" {
  description = "Org-wide voice caller ID Everbridge requires on every notification (senderCallerInfos.callerId). A live dialable number, so it lives in the gitignored terraform.tfvars rather than in source. Empty default — backend/everbridge.py raises at payload-build time when unset, before the EB POST."
  type        = string
  default     = ""
}

variable "everbridge_category_incounty" {
  description = "Everbridge category ID for in-county call-outs. An org-specific Everbridge record ID — kept in the gitignored terraform.tfvars, not in source. Empty default; backend/main.py::_category_for raises when unset."
  type        = string
  default     = ""
}

variable "everbridge_category_mutualaid" {
  description = "Everbridge category ID for mutual-aid call-outs. Same handling as everbridge_category_incounty."
  type        = string
  default     = ""
}

variable "everbridge_deliver_paths" {
  description = "JSON array of Everbridge delivery-path records, each needing both `id` and `pathId` (Phase 0 Key Discovery #2 — without `id`, EB returns HTTP 400 'No DeliveryPath for notification'). Org-specific record IDs, so kept in the gitignored terraform.tfvars. Empty default; backend/everbridge.py raises at payload-build time, before the EB POST."
  type        = string
  default     = ""
}

variable "everbridge_suppressed_group_ids" {
  description = "Comma-separated Everbridge group IDs that must never be paged (e.g. an admin/records group). Org-specific record IDs. Empty is legitimate and means suppress nothing, so this one does NOT fail loud."
  type        = string
  default     = ""
}

variable "everbridge_org_id" {
  description = "Everbridge organization ID for this deployment, used in every EB API path. Org-specific — kept in the gitignored terraform.tfvars. Empty default; main.py logs a startup warning and the EB calls fail visibly."
  type        = string
  default     = ""
}
