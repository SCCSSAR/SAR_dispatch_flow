#!/usr/bin/env bash
# rotate-secret.sh — atomically add a new Secret Manager version AND update
# the rotated/period_days labels so the date never drifts from the version.
#
# Usage: bin/rotate-secret.sh <secret-name> <data-file> <period-days>
# Env:   PROJECT (required) — GCP project ID
#
# Example (personal-dev — Phase 1):
#   PROJECT=your-dev-project-id bin/rotate-secret.sh \
#     slack-bot-token /tmp/new-token.txt 365
# Example (SCCSSAR-dev — Phase 5):
#   PROJECT=your-team-project-id bin/rotate-secret.sh \
#     slack-bot-token /tmp/new-token.txt 365
#
# The wrapper exists so rotation date and version always move together. Calling
# `gcloud secrets versions add` directly leaves the labels stale; the labels
# back the dispatcher-facing rotation banner (design Section 7.2 — surfacing
# rotation state to dispatcher). Always rotate via this script.
set -euo pipefail

SECRET="${1:?secret name required}"
DATA_FILE="${2:?data file required}"
PERIOD_DAYS="${3:?period days required}"
PROJECT="${PROJECT:?PROJECT env var required}"

gcloud secrets versions add "$SECRET" --data-file="$DATA_FILE" --project "$PROJECT"
gcloud secrets update "$SECRET" \
  --update-labels="rotated=$(date +%F),period_days=$PERIOD_DAYS" \
  --project "$PROJECT"

echo "✓ $SECRET rotated; period_days=$PERIOD_DAYS"
