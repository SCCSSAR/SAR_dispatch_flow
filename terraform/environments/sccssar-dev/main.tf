terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
  # Remote state in GCS — PR-K2 (security review finding #18, Phase 2 mirror
  # of PR-K which migrated personal-dev). Bucket created out-of-band by the
  # runbook in docs/ops-runbook.md ("Terraform State Management" → "Phase 2:
  # SCCSSAR-dev migration") with versioning + uniform bucket-level access +
  # public-access prevention. See that section before running `terraform
  # init` on a fresh clone.
  backend "gcs" {
    bucket = "sar-dispatch-sccssar-dev-tfstate"
    prefix = "dispatch-console"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# ---------------------------------------------------------------------------
# Enable required APIs
# ---------------------------------------------------------------------------

resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "aiplatform.googleapis.com",
    "firestore.googleapis.com",
    "secretmanager.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudtasks.googleapis.com", # Phase 5 PR-1 — Everbridge poll handler self-scheduling chain
    "compute.googleapis.com",    # Phase 5 PR-2 — Required for CIS 3.7 firewall-rule audit log source
    "logging.googleapis.com",    # Phase 5 PR-2 — Aikido CIS log-based metrics
    "monitoring.googleapis.com", # Phase 5 PR-2 — Aikido CIS alert policies + notification channel
  ])
  service            = each.value
  disable_on_destroy = false
}

# ---------------------------------------------------------------------------
# Artifact Registry — Docker image repository
# ---------------------------------------------------------------------------

resource "google_artifact_registry_repository" "dispatch" {
  repository_id = "dispatch-console"
  format        = "DOCKER"
  location      = var.region
  description   = "SCCSSAR Dispatch Console container images"

  # DESIGN DECISION (do not revert without team discussion):
  # keep-last-2-versions matches the Cloud Run revision retention (build scripts keep 2
  # revisions). Keeping only 1 image (latest) means the prior revision's image gets
  # garbage-collected after 7 days, making image-level rollback impossible.
  cleanup_policy_dry_run = false

  cleanup_policies {
    id     = "keep-last-2-versions"
    action = "KEEP"
    most_recent_versions {
      keep_count = 2
    }
  }

  cleanup_policies {
    id     = "delete-untagged-after-7-days"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "604800s"
    }
  }

  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------------------
# Service Account — dedicated SA for Cloud Run with minimal permissions
# ---------------------------------------------------------------------------

resource "google_service_account" "dispatch_runner" {
  account_id   = "dispatch-console-runner"
  display_name = "Dispatch Console Cloud Run SA"
  description  = "Runs the dispatch-console Cloud Run service. Minimum required permissions only."
}

# Vertex AI: generate content only (not admin)
resource "google_project_iam_member" "vertex_user" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.dispatch_runner.email}"
}

# Firestore: read/write for rate limiting counters
resource "google_project_iam_member" "firestore_user" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.dispatch_runner.email}"
}

# Cloud Logging: write logs
resource "google_project_iam_member" "log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.dispatch_runner.email}"
}

# ---------------------------------------------------------------------------
# Aikido security scanner — vendor-onboarded SA, role bindings TF-managed
# (PR-M2 — Phase 2)
#
# `security-reviewer-aikido@sar-dispatch-sccssar-dev.iam.gserviceaccount.com`
# is created out-of-band by Aikido's GCP integration flow (vendor-managed),
# so the SA itself is NOT declared here. The two project-level role
# bindings that grant Aikido's read-only access ARE declared here, per
# CLAUDE.md "Project-level IAM is Terraform-managed" — drift detection
# catches an accidental console-side privilege escalation (e.g. someone
# adding roles/editor instead of the two read-only roles Aikido needs).
#
# Live bindings already exist (Aikido was console-onboarded before this
# project's IAM-via-TF policy was established). `terraform apply` adopts
# them into state via idempotent SetIamPolicy patch — live policy is
# unchanged. Both roles are read-only:
#   - iam.securityReviewer: read IAM policies for security scanning
#   - viewer: read-only access to project resources for asset inventory
#
# This pattern (vendor-managed SA principal + TF-managed role bindings)
# is documented for future vendor onboardings (Datadog, etc.) in the
# IAM rule's "vendor-scanner SAs" guidance.
#
# Personal-dev does NOT have Aikido configured — only sccssar-dev is
# scanned. No corresponding block in terraform/environments/dev/.
resource "google_project_iam_member" "aikido_security_reviewer" {
  project = var.project_id
  role    = "roles/iam.securityReviewer"
  member  = "serviceAccount:security-reviewer-aikido@${var.project_id}.iam.gserviceaccount.com"
}

resource "google_project_iam_member" "aikido_viewer" {
  project = var.project_id
  role    = "roles/viewer"
  member  = "serviceAccount:security-reviewer-aikido@${var.project_id}.iam.gserviceaccount.com"
}

# Secret Manager — per-secret bindings (PR-N2: tightened from project-level
# secretAccessor; net-NEW per-secret viewer where this env never had project-
# level viewer at all — fixes a silent /version.token_health.d4h-access-token
# failure mode that has existed since PR #426 deployed here).
#
# secretAccessor: every secret the running Cloud Run service mounts via
#   secret_key_ref. List MUST stay in sync with the secret_key_ref blocks
#   in google_cloud_run_v2_service.dispatch.template.containers.env above —
#   if you add a new secret env there, add its ID here too.
#
# secretViewer: subset whose labels are read by backend/secret_health.py
#   (rotation/expiry banner). secretAccessor grants versions.access but NOT
#   secrets.get, so labels-via-get_secret needs viewer. List MUST stay in
#   sync with _MONITORED_SECRETS in backend/secret_health.py — including
#   currently-commented entries (everbridge-credentials, slack-bot-token)
#   so future un-comments are zero-IAM-change.
locals {
  dispatch_runner_accessor_secrets = toset([
    google_secret_manager_secret.authorized_emails.secret_id,
    google_secret_manager_secret.google_client_id.secret_id,
    "caltopo-team-id",
    "caltopo-credential-id",
    "caltopo-credential-secret",
    "everbridge-credentials",
    "dispatch-safe-list",
    "slack-bot-token",
    "slack-so-coordinator-email",
    "google-maps-api-key",
    "geoapify-api-key",
    "d4h-access-token",
  ])

  dispatch_runner_viewer_secrets = toset([
    "d4h-access-token",
    "everbridge-credentials",
    "slack-bot-token",
  ])
}

resource "google_secret_manager_secret_iam_member" "dispatch_runner_accessor" {
  for_each  = local.dispatch_runner_accessor_secrets
  project   = var.project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.dispatch_runner.email}"
}

resource "google_secret_manager_secret_iam_member" "dispatch_runner_viewer" {
  for_each  = local.dispatch_runner_viewer_secrets
  project   = var.project_id
  secret_id = each.value
  role      = "roles/secretmanager.viewer"
  member    = "serviceAccount:${google_service_account.dispatch_runner.email}"
}

# ---------------------------------------------------------------------------
# Secret Manager — store sensitive config values
# ---------------------------------------------------------------------------

resource "google_secret_manager_secret" "authorized_emails" {
  secret_id = "dispatch-authorized-emails"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "authorized_emails" {
  secret      = google_secret_manager_secret.authorized_emails.id
  secret_data = var.authorized_dispatcher_emails
}

resource "google_secret_manager_secret" "google_client_id" {
  secret_id = "dispatch-google-client-id"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "google_client_id" {
  secret      = google_secret_manager_secret.google_client_id.id
  secret_data = var.google_client_id
}

# ---------------------------------------------------------------------------
# Secret Manager — external secrets (PR-L2 — Phase 2 mirror of PR-L)
#
# Containers declared here; secret VERSIONS are managed out-of-band via
# `gcloud secrets versions add` (rotation tooling). Likewise LABELS
# (period_days, rotated) are mutated by rotation tooling — ignored here
# so plans stay clean across rotations. Cloud Run mounts these via the
# string `secret_key_ref` blocks below.
#
# Note on cross-env values: 8 of these 10 secrets hold the SAME payload
# across personal-dev and sccssar-dev (single third-party tenants for
# CalTopo, Everbridge, Slack, D4H). `google-maps-api-key` and
# `geoapify-api-key` are the two that differ — both are per-GCP-project
# external-vendor keys (billing/quota/restrictions attach to each
# project's own key; Geoapify uses one key per env — test on personal-dev,
# prod here). Mode-flag gating (`EVERBRIDGE_MODE=safe` etc.) is what
# isolates personal-dev runtime, not credential separation.
# ---------------------------------------------------------------------------

resource "google_secret_manager_secret" "caltopo_team_id" {
  secret_id = "caltopo-team-id"
  replication {
    auto {}
  }
  lifecycle {
    ignore_changes = [labels]
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "caltopo_credential_id" {
  secret_id = "caltopo-credential-id"
  replication {
    auto {}
  }
  lifecycle {
    ignore_changes = [labels]
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "caltopo_credential_secret" {
  secret_id = "caltopo-credential-secret"
  replication {
    auto {}
  }
  lifecycle {
    ignore_changes = [labels]
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "everbridge_credentials" {
  secret_id = "everbridge-credentials"
  replication {
    auto {}
  }
  lifecycle {
    ignore_changes = [labels]
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "dispatch_safe_list" {
  secret_id = "dispatch-safe-list"
  replication {
    auto {}
  }
  lifecycle {
    ignore_changes = [labels]
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "slack_bot_token" {
  secret_id = "slack-bot-token"
  replication {
    auto {}
  }
  lifecycle {
    ignore_changes = [labels]
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "slack_so_coordinator_email" {
  secret_id = "slack-so-coordinator-email"
  replication {
    auto {}
  }
  lifecycle {
    ignore_changes = [labels]
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "google_maps_api_key" {
  secret_id = "google-maps-api-key"
  replication {
    auto {}
  }
  lifecycle {
    ignore_changes = [labels]
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "geoapify_api_key" {
  secret_id = "geoapify-api-key"
  replication {
    auto {}
  }
  lifecycle {
    ignore_changes = [labels]
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret" "d4h_access_token" {
  secret_id = "d4h-access-token"
  replication {
    auto {}
  }
  lifecycle {
    ignore_changes = [labels]
  }
  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------------------
# Cloud Run — backend API service
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "dispatch" {
  name     = var.cloud_run_service_name
  location = var.region

  template {
    service_account = google_service_account.dispatch_runner.email

    # Limit concurrency — OCR calls are expensive; cap parallelism per instance
    max_instance_request_concurrency = 5

    scaling {
      min_instance_count = 0 # Scale to zero when idle
      max_instance_count = 3 # Hard cap — prevents runaway scaling costs
    }

    containers {
      # Image path — update after first docker push
      image = "${var.region}-docker.pkg.dev/${var.project_id}/dispatch-console/dispatch-console:latest"

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
      }

      env {
        name  = "GCP_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GCP_REGION"
        value = var.region
      }
      env {
        name  = "GEMINI_MODEL"
        value = var.gemini_model
      }
      env {
        name  = "LOG_LEVEL"
        value = var.log_level
      }
      env {
        name  = "ALLOWED_ORIGINS"
        value = var.allowed_origins
      }
      env {
        name  = "OCR_RATE_LIMIT_PER_MINUTE"
        value = tostring(var.ocr_rate_limit_per_minute)
      }
      env {
        name  = "OCR_RATE_LIMIT_PER_HOUR"
        value = tostring(var.ocr_rate_limit_per_hour)
      }
      env {
        name  = "OCR_RATE_LIMIT_PER_DAY"
        value = tostring(var.ocr_rate_limit_per_day)
      }
      env {
        name  = "OCR_DAILY_GLOBAL_CAP"
        value = tostring(var.ocr_daily_global_cap)
      }
      env {
        name  = "MAX_UPLOAD_SIZE_MB"
        value = tostring(var.max_upload_size_mb)
      }

      # Sensitive values from Secret Manager
      env {
        name = "AUTHORIZED_EMAILS"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.authorized_emails.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "GOOGLE_CLIENT_ID"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.google_client_id.secret_id
            version = "latest"
          }
        }
      }

      # CalTopo Team API credentials — created manually in Secret Manager (outside Terraform)
      # Team ID: CKPL11 | Credential ID: FNS02QQK7H38
      # See docs/deployment-guide.md §CalTopo secrets for creation instructions
      env {
        name = "CALTOPO_TEAM_ID"
        value_source {
          secret_key_ref {
            secret  = "caltopo-team-id"
            version = "latest"
          }
        }
      }
      env {
        name = "CALTOPO_CREDENTIAL_ID"
        value_source {
          secret_key_ref {
            secret  = "caltopo-credential-id"
            version = "latest"
          }
        }
      }
      env {
        name = "CALTOPO_CREDENTIAL_SECRET"
        value_source {
          secret_key_ref {
            secret  = "caltopo-credential-secret"
            version = "latest"
          }
        }
      }
      # Google Maps Geocoding API — fallback when Nominatim fails (e.g. misspelled street names).
      # Secret must be created manually before terraform apply:
      #   gcloud secrets create google-maps-api-key --project sar-dispatch-sccssar-dev
      #   echo -n "YOUR_KEY" | gcloud secrets versions add google-maps-api-key --data-file=- --project sar-dispatch-sccssar-dev
      # Enable "Geocoding API" on the key at console.cloud.google.com → APIs & Services → Credentials.
      env {
        name = "GOOGLE_MAPS_API_KEY"
        value_source {
          secret_key_ref {
            secret  = "google-maps-api-key"
            version = "latest"
          }
        }
      }

      # Google Docs / Drive API — no server-side credentials needed.
      # "Working Notes" docs are created using the dispatcher's own OAuth access token
      # (drive.file scope), forwarded from the frontend to /create-doc. The dispatcher's
      # Google account owns the doc; no service account or secret is required.
      # Pre-requisite (one-time, manual): enable Google Docs API and Google Drive API in
      # GCP Console → APIs & Services for this project.
      # GDOCS_SERVICE_ACCOUNT_KEY env var removed — not used by the dispatcher-OAuth approach.

      # ---------------------------------------------------------------------
      # Everbridge + Slack integration (Phase 5 SCCSSAR-dev mirror — PR-1)
      #
      # DESIGN DECISION (do not revert without team discussion):
      # Unlike personal-dev where EVERBRIDGE_MODE is hardcoded "safe" as a
      # safety net, SCCSSAR-dev drives both modes from variables. The empty-
      # string defaults keep the feature hidden until terraform.tfvars sets
      # them in PR-4. See variables.tf for the full PR-by-PR flag schedule.
      # ---------------------------------------------------------------------
      env {
        name  = "EVERBRIDGE_MODE"
        value = var.everbridge_mode
      }
      env {
        name  = "SLACK_MODE"
        value = var.slack_mode
      }
      env {
        name  = "ACTIVE_INCIDENTS_CHANNEL_ID"
        value = var.active_incidents_channel_id
      }
      # Admin-curated Slack user group whose members are pre-added to every
      # incident channel (issue #579). Replaces sourcing pre-adds from
      # #active-incidents membership (an open watch-channel — lurkers leaked
      # into every incident). Read in slack.py get_active_incident_management_members.
      env {
        name  = "ACTIVE_INCIDENT_MGMT_USERGROUP_ID"
        value = var.active_incident_mgmt_usergroup_id
      }







      # Cloud Tasks plumbing for the Everbridge + Slack polling chain.
      # PROJECT_ID is needed for the queue path
      # `projects/{p}/locations/{loc}/queues/{q}`; CLOUD_RUN_SERVICE_URL
      # is the base URL each enqueued task targets (`/poll-incident/{id}`,
      # `/delete-template/{id}`). Cloud Run does not auto-inject the public
      # URL — it's assigned at deploy time, so we set it here.
      env {
        name  = "PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "CLOUD_RUN_SERVICE_URL"
        value = "https://dispatch-console-${var.project_number}.${var.region}.run.app"
      }

      # Everbridge + Slack secrets — created manually in Secret Manager via
      # bin/rotate-secret.sh BEFORE this Terraform apply. Cloud Run rejects
      # revisions whose secret bindings reference non-existent secrets, so
      # the four secrets below must be created + populated first:
      #   bin/rotate-secret.sh sccssar-dev everbridge-credentials
      #   bin/rotate-secret.sh sccssar-dev dispatch-safe-list
      #   bin/rotate-secret.sh sccssar-dev slack-bot-token
      #   bin/rotate-secret.sh sccssar-dev slack-so-coordinator-email
      # Slack workspace + bot token are SAME as personal-dev (resolved
      # 2026-05-03) — copy values from `sar-dispatch-dev` Secret Manager.
      env {
        name = "EVERBRIDGE_CREDENTIALS"
        value_source {
          secret_key_ref {
            secret  = "everbridge-credentials"
            version = "latest"
          }
        }
      }
      env {
        name = "DISPATCH_SAFE_LIST"
        value_source {
          secret_key_ref {
            secret  = "dispatch-safe-list"
            version = "latest"
          }
        }
      }
      env {
        name = "SLACK_BOT_TOKEN"
        value_source {
          secret_key_ref {
            secret  = "slack-bot-token"
            version = "latest"
          }
        }
      }
      env {
        name = "SLACK_SO_COORDINATOR_EMAIL"
        value_source {
          secret_key_ref {
            secret  = "slack-so-coordinator-email"
            version = "latest"
          }
        }
      }

      # D4H Team Manager API — service-account PAT for SCCSSAR team 1775.
      # Initialize once (first time only) BEFORE terraform apply:
      #   gcloud secrets create d4h-access-token --project sar-dispatch-sccssar-dev
      #   echo -n "YOUR_PAT" > /tmp/d4h-pat.txt
      #   PROJECT=sar-dispatch-sccssar-dev bin/rotate-secret.sh d4h-access-token /tmp/d4h-pat.txt 365
      #   rm /tmp/d4h-pat.txt
      # Rotate before expiry (always via rotate-secret.sh, never bare gcloud):
      #   echo -n "NEW_PAT" > /tmp/d4h-pat.txt
      #   PROJECT=sar-dispatch-sccssar-dev bin/rotate-secret.sh d4h-access-token /tmp/d4h-pat.txt 365
      #   rm /tmp/d4h-pat.txt
      # PAT scope requirements: see GH issue #419 (Incident.CREATE/UPDATE/READ,
      # ActivityAttendance.READ/UPDATE/CREATE; *.DELETE explicitly off). Expiry
      # monitored by backend secret_health.py; yellow banner in Dispatch within
      # 30 days of expiry. The PAT itself is generated server-side by the D4H
      # team admin — SAME D4H team (1775) as personal-dev, so the same PAT
      # value works in both projects.
      env {
        name = "D4H_ACCESS_TOKEN"
        value_source {
          secret_key_ref {
            secret  = "d4h-access-token"
            version = "latest"
          }
        }
      }

      # OIDC service-account email used by Cloud Tasks to sign requests sent
      # to the /d4h-sync-yes worker endpoint. Re-uses the everbridge-poll-sa
      # (the same SA that signs /poll-incident enqueues) since the OIDC
      # subject doesn't have to be queue-specific. The d4h.py module reads
      # this env var directly (NOT derived from PROJECT_ID like the EB poll
      # path); see _enqueue_d4h_task.
      env {
        name  = "CLOUD_TASKS_SERVICE_ACCOUNT"
        value = google_service_account.everbridge_poll.email
      }

      # Geoapify Places API key + staging config. sccssar-dev runs Geoapify-PRIMARY
      # with a SEQUENTIAL Overpass fallback (Overpass→Geoapify migration): the
      # personal-dev parallel-shadow soak validated compatibility/latency, so here
      # Geoapify drives (STAGING_SOURCE=geoapify) and Overpass is called only if
      # Geoapify fails (STAGING_SHADOW=off) — no parallel shadow, which also avoids
      # re-imposing Overpass latency on every /ocr. Still gated on `terraform apply`
      # + `build-sccssar-dev.sh` (operator-run), so this reaches real dispatchers only
      # on an explicit build. One key per env: sccssar-dev uses the Geoapify PROD key.
      # Secret must be created BEFORE this apply (Cloud Run rejects a revision binding
      # a non-existent secret):
      #   gcloud secrets create geoapify-api-key --project sar-dispatch-sccssar-dev
      #   echo -n "PROD_KEY" | gcloud secrets versions add geoapify-api-key \
      #     --data-file=- --project sar-dispatch-sccssar-dev
      # (New env blocks go at the END of this list — blocks are positional in the diff.)
      env {
        name = "GEOAPIFY_API_KEY"
        value_source {
          secret_key_ref {
            secret  = "geoapify-api-key"
            version = "latest"
          }
        }
      }
      env {
        name  = "STAGING_SOURCE"
        value = "geoapify"
      }
      env {
        name  = "STAGING_SHADOW"
        value = "off"
      }

      # ---- Everbridge organisation record IDs --------------------------
      # APPENDED AT THE END ON PURPOSE. Cloud Run `env` is an ORDERED LIST to
      # Terraform, so inserting a block mid-list renumbers every block after
      # it and the plan renders as a wall of renames ("PROJECT_ID" ->
      # "EVERBRIDGE_CALLER_ID"). The net result is the same, but a real
      # mistake would be invisible inside that diff. Appending keeps the plan
      # readable: N additions and nothing else. Add new vars HERE.
      # Org-wide voice caller ID (see variables.tf). Redacted from source; the
      # value lives in the gitignored terraform.tfvars.
      env {
        name  = "EVERBRIDGE_CALLER_ID"
        value = var.everbridge_caller_id
      }
      env {
        name  = "EVERBRIDGE_ORG_ID"
        value = var.everbridge_org_id
      }
      # Everbridge org record IDs — redacted from source, values in the
      # gitignored terraform.tfvars. See variables.tf for each one's failure mode.
      env {
        name  = "EVERBRIDGE_CATEGORY_INCOUNTY"
        value = var.everbridge_category_incounty
      }
      env {
        name  = "EVERBRIDGE_CATEGORY_MUTUALAID"
        value = var.everbridge_category_mutualaid
      }
      env {
        name  = "EVERBRIDGE_DELIVER_PATHS"
        value = var.everbridge_deliver_paths
      }
      env {
        name  = "EVERBRIDGE_SUPPRESSED_GROUP_IDS"
        value = var.everbridge_suppressed_group_ids
      }


      startup_probe {
        http_get { path = "/health" }
        initial_delay_seconds = 10 # give cold start time to pull image + load Python deps
        period_seconds        = 5
        failure_threshold     = 8 # up to 50s total (10 + 8*5) for worst-case cold start
      }

      liveness_probe {
        http_get { path = "/health" }
        period_seconds    = 60 # check every 60s (was 30s)
        timeout_seconds   = 10 # allow 10s for health response (was 1s — too tight during Gemini calls)
        failure_threshold = 5  # require 5 consecutive failures before kill (was 3 = ~90s kill window)
        # Net effect: instance survives up to 5 minutes of slow responses before being killed.
        # Two-pass Gemini OCR takes 30–60s; this prevents liveness kills during normal long requests.
      }
    }
  }

  depends_on = [
    google_project_service.apis,
    google_artifact_registry_repository.dispatch,
    google_secret_manager_secret_version.authorized_emails,
    google_secret_manager_secret_version.google_client_id,
  ]
}

# ---------------------------------------------------------------------------
# Cloud Run IAM — allow unauthenticated access so browsers can load the app
#
# Security model:
#   GET /        → public (serves the HTML page — not sensitive data)
#   GET /health  → public (Cloud Run probe)
#   POST /ocr    → FastAPI enforces Google ID token + email allowlist
#
# allUsers invoker is required so the browser can load index.html without a
# GCP identity token. The OCR endpoint is protected at the application layer
# (FastAPI auth.py) as defense-in-depth — the platform layer alone is not
# sufficient because the browser-side Google Sign-In token is not a Cloud Run
# identity token and cannot be used for platform-level auth.
#
# IMPORTANT: If this resource fails with "One or more users named in the policy
# do not belong to a permitted customer", the GCP org has the constraint
# constraints/iam.allowedPolicyMemberTypes set. An org admin must apply the
# restoreDefault override at the project level before terraform apply will
# succeed. See docs/deployment-guide.md §Org Policy for exact steps.
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service_iam_member" "public_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.dispatch.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ---------------------------------------------------------------------------
# Firestore — native mode database for rate limiting counters
# ---------------------------------------------------------------------------

resource "google_firestore_database" "default" {
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  depends_on = [google_project_service.apis]
}

# Firestore TTL on incidents.expire_at — auto-delete docs 24h after polling closes.
# DESIGN DECISION (do not revert without team discussion): the Firestore
# `incidents` collection is the SINGLE allowed PII store for the Everbridge +
# Slack integration. TTL is the privacy control that bounds retention to ~25h
# (24h after stop + up to 4h polling). See backend/incidents.py module docstring
# + design Section 7.4. Mirrored from personal-dev as part of Phase 5 PR-1.
resource "google_firestore_field" "incidents_ttl" {
  project    = var.project_id
  database   = google_firestore_database.default.name
  collection = "incidents"
  field      = "expire_at"
  ttl_config {}
}

# ---------------------------------------------------------------------------
# Audit Logging — Data Access logs for IAM + Cloud Resource Manager
# Aikido findings: #262 (Project Ownership logging) + #264 (Audit Config logging)
# ---------------------------------------------------------------------------

resource "google_project_iam_audit_config" "project_ownership" {
  project = var.project_id
  service = "cloudresourcemanager.googleapis.com"
  audit_log_config {
    log_type = "DATA_WRITE"
  }
  audit_log_config {
    log_type = "DATA_READ"
  }
}

resource "google_project_iam_audit_config" "iam_audit_config" {
  project = var.project_id
  service = "iam.googleapis.com"
  audit_log_config {
    log_type = "ADMIN_READ"
  }
  audit_log_config {
    log_type = "DATA_READ"
  }
  audit_log_config {
    log_type = "DATA_WRITE"
  }
}

# ---------------------------------------------------------------------------
# Cloud Tasks — Everbridge poll handler self-scheduling chain
# (Phase 5 SCCSSAR-dev mirror — PR-1)
#
# /poll-incident/{event_id} is invoked by Cloud Tasks (NOT by browsers or
# dispatcher Google ID tokens). Cloud Tasks signs an OIDC token as
# everbridge_poll SA on each task; the endpoint validates the token's email
# matches that SA's email. This is the only auth path that can reach the
# endpoint successfully — it cannot be invoked from a browser.
# ---------------------------------------------------------------------------

resource "google_service_account" "everbridge_poll" {
  account_id   = "everbridge-poll-sa"
  display_name = "Everbridge Poll OIDC SA"
  description  = "Cloud Tasks signs OIDC tokens as this SA when invoking /poll-incident."
}

# Polling SA needs run.invoker so Cloud Tasks → Cloud Run calls authenticate.
resource "google_cloud_run_v2_service_iam_member" "everbridge_poll_invoker" {
  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.dispatch.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.everbridge_poll.email}"
}

# Dispatch runner SA needs to enqueue tasks (called from /send-notification +
# /poll-incident to schedule the next poll, and from per-YES sync to enqueue
# D4H attendance updates).
#
# PR-N2: tightened from project-level cloudtasks.enqueuer to per-queue. List
# MUST stay in sync with the google_cloud_tasks_queue resources below — if
# you add a new queue, add it here too.
resource "google_cloud_tasks_queue_iam_member" "dispatch_runner_enqueuer" {
  for_each = toset([
    google_cloud_tasks_queue.everbridge_poll.name,
    google_cloud_tasks_queue.d4h_per_yes_sync.name,
  ])
  project  = var.project_id
  location = var.region
  name     = each.value
  role     = "roles/cloudtasks.enqueuer"
  member   = "serviceAccount:${google_service_account.dispatch_runner.email}"
}

# Dispatch runner SA needs TWO bindings to attach an OIDC token to a Cloud
# Task that will fire as the polling SA:
#   1. roles/iam.serviceAccountTokenCreator — grants iam.serviceAccounts.signJwt
#      and iam.serviceAccounts.getOpenIdToken (needed to sign the OIDC token).
#   2. roles/iam.serviceAccountUser — grants iam.serviceAccounts.actAs (needed
#      to "act as" the polling SA when configuring the task; Cloud Tasks
#      validates this permission specifically when CreateTask is called with
#      an oidc_token field).
#
# Both bindings are needed; they grant different permissions and don't
# supersede each other. Personal-dev hit a PERMISSION_DENIED on its first
# live test (2026-04-27) when binding #2 was missing — surface here pre-empts
# the same on SCCSSAR-dev.
resource "google_service_account_iam_member" "dispatch_can_act_as_poll" {
  service_account_id = google_service_account.everbridge_poll.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.dispatch_runner.email}"
}

resource "google_service_account_iam_member" "dispatch_user_of_poll" {
  service_account_id = google_service_account.everbridge_poll.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.dispatch_runner.email}"
}

# The Cloud Tasks queue itself.
resource "google_cloud_tasks_queue" "everbridge_poll" {
  name     = "everbridge-poll-queue"
  location = var.region

  rate_limits {
    # 5 dispatches/sec is well above the 1/sec polling cadence (15s interval =
    # ~0.07/sec sustained). Headroom for burst on incident creation +
    # post-stop final tally edits.
    max_dispatches_per_second = 5
    max_concurrent_dispatches = 10
  }

  retry_config {
    # 3 attempts is conservative — Cloud Tasks retries failed deliveries within
    # this budget, then drops the task. Dropping is acceptable because the
    # /poll-incident handler always re-enqueues the next poll regardless of
    # whether THIS poll succeeded; the chain is self-healing on the next cycle.
    max_attempts  = 3
    min_backoff   = "5s"
    max_backoff   = "30s"
    max_doublings = 2
  }

  depends_on = [google_project_service.apis]
}

# Cloud Tasks queue for D4H per-YES attendance sync. Each YES-reply in
# Everbridge triggers a deterministic-task-name enqueue here (`yes-{event_id}-
# {safe_email}`); the worker /d4h-sync-yes POSTs an ATTENDING record to D4H.
#
# Selective-mode architecture (CLAUDE.md Locked Decision 2026-05-19): there
# is NO bulk-ABSENT queue — attendance starts empty at incident creation and
# fills only as responders YES-reply. This queue is the only D4H Cloud Tasks
# queue in the system.
#
# Tuning notes (mirrored from personal-dev):
#   max_dispatches_per_second = 20  — handles burst of multiple YES arrivals
#   max_concurrent_dispatches = 10  — limits concurrent D4H API load
#   max_attempts = 5                — covers transient D4H 5xx; ample for the
#                                     typical 1-2 retry recovery seen in spike 11
#   max_retry_duration = 1800s      — 30min budget (well within Cloud Tasks 24h
#                                     dedupe window for the deterministic task name)
resource "google_cloud_tasks_queue" "d4h_per_yes_sync" {
  name     = "d4h-per-yes-sync"
  location = var.region

  rate_limits {
    max_dispatches_per_second = 20
    max_concurrent_dispatches = 10
  }

  retry_config {
    max_attempts       = 5
    max_retry_duration = "1800s"
    min_backoff        = "10s"
    max_backoff        = "300s"
    max_doublings      = 5
  }

  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------------------
# CIS Log-based Metrics + Alert Policies
# (Phase 5 SCCSSAR-dev mirror — PR-2)
#
# Aikido findings closed by these resources:
#   CIS GCP 2.4 — Project Ownership assignments
#   CIS GCP 2.5 — Audit Configuration changes
#   CIS GCP 3.7 — VPC Firewall Rule changes
#
# Pattern: log filter → user metric → alert policy → email notification.
# These fire on *changes* to the named resource, not on normal reads.
#
# Note: the audit_log_config blocks earlier in this file enable the underlying
# log STORAGE (Aikido findings #262 + #264). The resources below are the
# DETECTION layer that alerts on entries written to those logs.
# ---------------------------------------------------------------------------

resource "google_monitoring_notification_channel" "security_alerts" {
  project      = var.project_id
  display_name = "Security Alert Email"
  type         = "email"

  labels = {
    email_address = var.alert_notification_email
  }

  depends_on = [google_project_service.apis]
}

# CIS 2.4 — alert on any project ownership (roles/owner) assignment or removal

resource "google_logging_metric" "project_ownership_changes" {
  project = var.project_id
  name    = "project-ownership-changes"
  filter  = <<-EOT
    (protoPayload.serviceName="cloudresourcemanager.googleapis.com") AND (
      ProjectOwnership OR projectOwnerInvitee OR
      (protoPayload.serviceData.policyDelta.bindingDeltas.action="ADD" AND
       protoPayload.serviceData.policyDelta.bindingDeltas.role="roles/owner") OR
      (protoPayload.serviceData.policyDelta.bindingDeltas.action="REMOVE" AND
       protoPayload.serviceData.policyDelta.bindingDeltas.role="roles/owner")
    )
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "project_ownership_changes" {
  project      = var.project_id
  display_name = "CIS 2.4 — Project Ownership Changes"
  combiner     = "OR"

  conditions {
    display_name = "Any project ownership change detected"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.project_ownership_changes.name}\" AND resource.type=\"global\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_RATE"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.security_alerts.name]

  documentation {
    content   = "A project ownership change was detected in `${var.project_id}`. Review: https://console.cloud.google.com/iam-admin/iam?project=${var.project_id}"
    mime_type = "text/markdown"
  }

  depends_on = [google_project_service.apis]
}

# CIS 2.5 — alert on any change to Cloud Audit logging configuration

resource "google_logging_metric" "audit_config_changes" {
  project = var.project_id
  name    = "audit-config-changes"
  filter  = "protoPayload.methodName=\"SetIamPolicy\" AND protoPayload.serviceData.policyDelta.auditConfigDeltas:*"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "audit_config_changes" {
  project      = var.project_id
  display_name = "CIS 2.5 — Audit Configuration Changes"
  combiner     = "OR"

  conditions {
    display_name = "Any audit configuration change detected"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.audit_config_changes.name}\" AND resource.type=\"global\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_RATE"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.security_alerts.name]

  documentation {
    content   = "An audit configuration change was detected in `${var.project_id}`. Review: https://console.cloud.google.com/iam-admin/audit?project=${var.project_id}"
    mime_type = "text/markdown"
  }

  depends_on = [google_project_service.apis]
}

# CIS 3.7 — alert on any VPC firewall rule insert, patch, or delete

resource "google_logging_metric" "firewall_rule_changes" {
  project = var.project_id
  name    = "firewall-rule-changes"
  filter  = "resource.type=\"gce_firewall_rule\" AND (protoPayload.methodName:\"compute.firewalls.patch\" OR protoPayload.methodName:\"compute.firewalls.insert\" OR protoPayload.methodName:\"compute.firewalls.delete\")"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "firewall_rule_changes" {
  project      = var.project_id
  display_name = "CIS 3.7 — VPC Firewall Rule Changes"
  combiner     = "OR"

  conditions {
    display_name = "Any VPC firewall rule change detected"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.firewall_rule_changes.name}\" AND resource.type=\"global\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_RATE"
      }
    }
  }

  notification_channels = [google_monitoring_notification_channel.security_alerts.name]

  documentation {
    content   = "A VPC firewall rule change was detected in `${var.project_id}`. Review: https://console.cloud.google.com/networking/firewalls/list?project=${var.project_id}"
    mime_type = "text/markdown"
  }

  depends_on = [google_project_service.apis]
}
