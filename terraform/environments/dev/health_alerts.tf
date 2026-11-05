# Health alerts for the dispatch-console service (Bill, 2026-11-05).
#
# Log-based metrics plus an email alert on ANY occurrence, on the existing
# security_alerts channel. Replaces the lapsed #519 OOM briefing routine,
# which only reminded someone to run a check by hand. Every filter is pinned
# to the code that writes the log line by TestHealthAlertFilters in
# backend/test_main_regression.py, and this file must stay identical in both
# environments. Proven read-only against 35 days of logs before it was written.

resource "google_logging_metric" "rate_limit_txn_expired" {
  project = var.project_id
  name    = "dispatch-rate-limit-txn-expired"
  filter  = <<-EOT
    resource.type="cloud_run_revision" AND resource.labels.service_name="dispatch-console"
    AND jsonPayload.logger="rate_limit" AND jsonPayload.message:"transaction expired"
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "rate_limit_txn_expired" {
  project      = var.project_id
  display_name = "Dispatch Turbo — Firestore rate-limit transaction expired"
  combiner     = "OR"

  conditions {
    display_name = "Any rate-limit transaction expiry"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.rate_limit_txn_expired.name}\" AND resource.type=\"cloud_run_revision\""
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
    content   = "The rate limiter's Firestore transaction expired in `${var.project_id}` (SCCSSAR/SAR_dispatch_flow#92). A retry usually recovers it. **If an `Unhandled exception` alert fired at the same time, every retry ran out and the request returned 500.** Read the lines around it in Logs Explorer: https://console.cloud.google.com/logs/query;query=resource.type%3D%22cloud_run_revision%22?project=${var.project_id}"
    mime_type = "text/markdown"
  }

  depends_on = [google_project_service.apis]
}

resource "google_logging_metric" "unhandled_exception" {
  project = var.project_id
  name    = "dispatch-unhandled-exception"
  filter  = <<-EOT
    resource.type="cloud_run_revision" AND resource.labels.service_name="dispatch-console"
    AND jsonPayload.logger="main" AND jsonPayload.message:"Unhandled exception:"
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "unhandled_exception" {
  project      = var.project_id
  display_name = "Dispatch Turbo — unhandled exception (500)"
  combiner     = "OR"

  conditions {
    display_name = "Any unhandled exception"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.unhandled_exception.name}\" AND resource.type=\"cloud_run_revision\""
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
    content   = "A Dispatch Turbo request in `${var.project_id}` raised an exception nothing caught, and returned 500. The log line names the exception class only. `InvalidArgument` next to a transaction-expired alert means the rate-limit retries ran out. Logs Explorer: https://console.cloud.google.com/logs/query;query=resource.type%3D%22cloud_run_revision%22?project=${var.project_id}"
    mime_type = "text/markdown"
  }

  depends_on = [google_project_service.apis]
}

resource "google_logging_metric" "memory_limit_exceeded" {
  project = var.project_id
  name    = "dispatch-memory-limit-exceeded"
  filter  = <<-EOT
    resource.type="cloud_run_revision" AND resource.labels.service_name="dispatch-console"
    AND textPayload:"Memory limit"
  EOT

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
  }
}

resource "google_monitoring_alert_policy" "memory_limit_exceeded" {
  project      = var.project_id
  display_name = "Dispatch Turbo — container out of memory (OOM)"
  combiner     = "OR"

  conditions {
    display_name = "Any Cloud Run memory-limit kill"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.memory_limit_exceeded.name}\" AND resource.type=\"cloud_run_revision\""
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
    content   = "A Dispatch Turbo instance in `${var.project_id}` exceeded its memory limit and was killed (OOM). Any request it was serving failed. Check what the instance was serving in the minutes before (ops#851, and #519 for the polling-load pattern). Logs Explorer: https://console.cloud.google.com/logs/query;query=resource.type%3D%22cloud_run_revision%22?project=${var.project_id}"
    mime_type = "text/markdown"
  }

  depends_on = [google_project_service.apis]
}
