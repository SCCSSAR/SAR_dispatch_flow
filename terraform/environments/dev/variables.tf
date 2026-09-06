variable "project_id" {
  description = "GCP project ID for the dev/test environment"
  type        = string
  default     = "sar-dispatch-dev"
}

variable "project_number" {
  description = "GCP project number for the dev/test environment"
  type        = string
  default     = "970461953836"
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
  # Bumped 50 → 250 on 2026-05-10 (5x) for personal-dev solo-testing capacity.
  # The counter is SHARED across /ocr, /create-map, /create-doc,
  # /send-notification, and /apply-staging-override — a single test run
  # (OCR + 2–3 override iterations + map + EB+Slack) burns ~6 slots, so 50
  # was too tight for end-of-PR-D iterative testing. SCCSSAR-dev keeps its
  # conservative 50/day for the multi-dispatcher production scenario.
  default     = 250
}

variable "ocr_daily_global_cap" {
  description = "Max OCR calls per day across all users (project-level hard ceiling)"
  type        = number
  # Bumped 200 → 1000 on 2026-05-10 (5x) — same rationale as the per-user
  # bump above. Personal-dev has one tester; 1000 still hard-caps runaway
  # cost while giving headroom for back-to-back test runs.
  default     = 1000
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
  # (10 sites — none load-bearing). When actively debugging on personal-dev,
  # set `log_level = "DEBUG"` in the gitignored terraform.tfvars and re-apply.
  default     = "INFO"
}

variable "allowed_origins" {
  description = "Comma-separated list of allowed CORS origins (frontend URL)"
  type        = string
  default     = "http://localhost:8080"
  # Update to Cloud Run frontend URL after first deploy
}

variable "cloud_run_service_name" {
  description = "Name of the Cloud Run service"
  type        = string
  default     = "dispatch-console"
}

# ---------------------------------------------------------------------------
# Everbridge + Slack integration (Phase 1 Task 1.1 — personal-dev only)
# See docs/plans/2026-04-24-everbridge-slack-integration-plan.md
#
# DESIGN DECISION (do not revert without team discussion):
# `everbridge_mode` is NOT a variable here — it's hardcoded `safe` in main.tf
# as the personal-dev safety net (CLAUDE.md locked decision). Even if a
# SCCSSAR-dev image is mistakenly deployed to personal-dev, the env var the
# new revision receives keeps safe mode on. To run Phase 4 Step 4b/4c (full
# mode testing on personal-dev), edit main.tf directly and apply — do NOT
# add an `everbridge_mode` variable here.
# ---------------------------------------------------------------------------

variable "slack_mode" {
  description = "Slack auto-invite mode: shadow (initial channel members invited; per-cycle would-invite messages, no per-responder invites) or full (per-responder invites enabled). Default shadow. Can be flipped to full via terraform.tfvars at Phase 4 Step 4c."
  type        = string
  default     = "shadow"
  validation {
    condition     = contains(["shadow", "full"], var.slack_mode)
    error_message = "slack_mode must be 'shadow' or 'full'."
  }
}

variable "staging_source" {
  description = "Staging POI source: overpass or geoapify. personal-dev defaults to geoapify for the Overpass→Geoapify migration soak (Geoapify drives; Overpass runs as the always-on shadow). Flip back to overpass via terraform.tfvars without a code change."
  type        = string
  default     = "geoapify"
  validation {
    condition     = contains(["overpass", "geoapify"], var.staging_source)
    error_message = "staging_source must be 'overpass' or 'geoapify'."
  }
}

variable "active_incidents_channel_id" {
  description = "Slack channel ID for #active-incidents (private; live tally posted here). Created manually in Slack UI; ID copied here. Empty default — must be set in terraform.tfvars before applying."
  type        = string
  default     = ""
}

variable "active_incident_mgmt_usergroup_id" {
  description = "Slack user-group ID for @active_incident_management (issue #579). Admin-curated members pre-added to every incident channel, replacing #active-incidents membership as the pre-add source. Same workspace as sccssar-dev — group ID is reused. Empty default — helper degrades to dispatcher + SO coordinator (best-effort) when unset."
  type        = string
  default     = ""
}

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
