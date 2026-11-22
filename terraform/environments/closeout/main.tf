# Close-out helper's Vertex project (SCCSSAR/closeout-helper).
#
# Model access only: Search Managers run Gemini CLI on their own laptops with
# their own Google sign-in (ADC). There is no Cloud Run service, no secret, no
# service account of ours and no network in this project. The project, billing
# link, budget and state bucket are created out-of-band before `terraform init`
# (ops repo, docs/closeout-vertex-project.md, steps 1-5).
#
# Mirrors sccssar-dev's hardening where it applies: IAM in Terraform, GCS state,
# the same audit configs, the same CIS log metrics and alerts, and the same
# Aikido bindings.

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
  # Bucket created out-of-band with versioning, uniform bucket-level access and
  # public access prevention, as for the other environments.
  backend "gcs" {
    bucket = "sar-closeout-sccssar-tfstate"
    prefix = "closeout-helper"
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

# ---------------------------------------------------------------------------
# APIs. compute.googleapis.com stays OFF: enabling it creates the default
# network with its open RDP and SSH rules, and nothing here needs a network.
# ---------------------------------------------------------------------------

resource "google_project_service" "apis" {
  for_each = toset([
    "aiplatform.googleapis.com",
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "serviceusage.googleapis.com",
    "storage.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# ---------------------------------------------------------------------------
# Search Managers: prediction only. roles/aiplatform.user would also let a
# user change the project's cache setting, manage models and endpoints, and
# create resources; none of that is needed to run the helper.
# ---------------------------------------------------------------------------

resource "google_project_iam_custom_role" "closeout_predict" {
  project     = var.project_id
  role_id     = "closeoutPredictOnly"
  title       = "Close-out helper: predict only"
  description = "Call Gemini on Vertex and read back the cache setting. Nothing else."
  permissions = [
    "aiplatform.endpoints.predict", # generateContent and countTokens on publisher models
    "aiplatform.cacheConfigs.get",  # the helper's disableCache read-back before each run
    "serviceusage.services.use",    # bill the calls to this project as the ADC quota project
  ]
}

resource "google_project_iam_member" "closeout_users" {
  for_each = toset(var.closeout_users)
  project  = var.project_id
  role     = google_project_iam_custom_role.closeout_predict.name
  member   = each.value
}

# ---------------------------------------------------------------------------
# Aikido: the SA is created by Aikido's GCP integration (vendor-managed), so
# only its two read-only bindings are declared, as on sccssar-dev. Apply after
# the integration exists, or these fail because the SA does not exist yet.
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Audit logging: the same two configs as sccssar-dev (Aikido #262, #264).
# Vertex data-access logs stay off, as on sccssar-dev, so prompts and
# responses never reach Cloud Logging.
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
# Detection: CIS log metrics with email alerts. 2.4, 2.5 and 3.7 use
# sccssar-dev's filters verbatim. 2.6 is added because this project has a
# custom role. 3.7 stays although compute is off: it fires if someone turns
# compute on and adds a firewall rule.
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

locals {
  cis_metrics = {
    "project-ownership-changes" = {
      title  = "CIS 2.4 — Project Ownership Changes"
      review = "iam-admin/iam"
      filter = <<-EOT
        (protoPayload.serviceName="cloudresourcemanager.googleapis.com") AND (
          ProjectOwnership OR projectOwnerInvitee OR
          (protoPayload.serviceData.policyDelta.bindingDeltas.action="ADD" AND
           protoPayload.serviceData.policyDelta.bindingDeltas.role="roles/owner") OR
          (protoPayload.serviceData.policyDelta.bindingDeltas.action="REMOVE" AND
           protoPayload.serviceData.policyDelta.bindingDeltas.role="roles/owner")
        )
      EOT
    }
    "audit-config-changes" = {
      title  = "CIS 2.5 — Audit Configuration Changes"
      review = "iam-admin/audit"
      filter = "protoPayload.methodName=\"SetIamPolicy\" AND protoPayload.serviceData.policyDelta.auditConfigDeltas:*"
    }
    "custom-role-changes" = {
      title  = "CIS 2.6 — Custom Role Changes"
      review = "iam-admin/roles"
      filter = "resource.type=\"iam_role\" AND (protoPayload.methodName=\"google.iam.admin.v1.CreateRole\" OR protoPayload.methodName=\"google.iam.admin.v1.DeleteRole\" OR protoPayload.methodName=\"google.iam.admin.v1.UpdateRole\")"
    }
    "firewall-rule-changes" = {
      title  = "CIS 3.7 — VPC Firewall Rule Changes"
      review = "networking/firewalls/list"
      filter = "resource.type=\"gce_firewall_rule\" AND (protoPayload.methodName:\"compute.firewalls.patch\" OR protoPayload.methodName:\"compute.firewalls.insert\" OR protoPayload.methodName:\"compute.firewalls.delete\")"
    }
  }
}

resource "google_logging_metric" "cis" {
  for_each = local.cis_metrics
  project  = var.project_id
  name     = each.key
  filter   = each.value.filter

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }

  depends_on = [google_project_service.apis]
}

resource "google_monitoring_alert_policy" "cis" {
  for_each     = local.cis_metrics
  project      = var.project_id
  display_name = each.value.title
  combiner     = "OR"

  conditions {
    display_name = each.value.title
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.cis[each.key].name}\" AND resource.type=\"global\""
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
    content   = "${each.value.title} detected in `${var.project_id}`. Review: https://console.cloud.google.com/${each.value.review}?project=${var.project_id}"
    mime_type = "text/markdown"
  }

  depends_on = [google_project_service.apis]
}
