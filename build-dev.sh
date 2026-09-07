#!/bin/bash
# =============================================================================
# build-dev.sh — Build, push, and deploy to the personal dev GCP project
#
# Usage:  bash build-dev.sh
#
# This script targets the environment deploy.env defines under ENV_NAME="dev"
# — the sandbox / staging project. Deploy here before the team project, always.
#
# Before running:
#   1. Confirm you're on the merged main branch (git log --oneline -1)
#   2. Update PHASE below if releasing a new phase
#   3. Ensure docker is running
#   4. Ensure gcloud is authenticated (gcloud auth list)
#
# See also: build-sccssar-dev.sh for the SCCSSAR team project.
# =============================================================================
set -e

# ---------------------------------------------------------------------------
# Project-specific settings — update PHASE when releasing a new phase
# ---------------------------------------------------------------------------
# Milestone string — number + theme name. Match the GitHub Milestone label at
# the repository's GitHub milestone labels (e.g. "1.6 Twin Peaks",
# "1.7 Accountant", "1.8 Slacker"). UPDATE this once per milestone bump.
PHASE="1.8 Slacker"

# ---------------------------------------------------------------------------
# Deployment settings — loaded from the gitignored deploy.env
#
# Project ID, OAuth client ID and service URL live in deploy.env, NOT here.
# .gitignore cannot protect a tracked file, so hardcoding them in this script
# would commit them on the next `git add`. Copy deploy.env.template to
# deploy.env and fill it in; see that file's header.
# ---------------------------------------------------------------------------
ENV_NAME="dev"

# ---------------------------------------------------------------------------
# Derived values — do not edit below this line
# ---------------------------------------------------------------------------
REPO_ROOT="$(git -C "$(cd "$(dirname "$0")" && pwd)" rev-parse --show-toplevel)"

DEPLOY_ENV="${REPO_ROOT}/deploy.env"
if [ ! -f "${DEPLOY_ENV}" ]; then
  echo "  ERROR: ${DEPLOY_ENV} not found." >&2
  echo "  Copy the template and fill in your own values:" >&2
  echo "    cp ${REPO_ROOT}/deploy.env.template ${DEPLOY_ENV}" >&2
  exit 1
fi
# shellcheck source=/dev/null
. "${DEPLOY_ENV}"

# Fail loud on anything deploy.env did not set. A blank PROJECT would otherwise
# deploy to whatever gcloud's active project happens to be.
PROJECT="${PROJECT:?deploy.env did not set PROJECT for ENV_NAME=${ENV_NAME}}"
OAUTH_CLIENT_ID="${OAUTH_CLIENT_ID:?deploy.env did not set OAUTH_CLIENT_ID for ENV_NAME=${ENV_NAME}}"
SERVICE_URL="${SERVICE_URL:?deploy.env did not set SERVICE_URL for ENV_NAME=${ENV_NAME}}"
DISPLAY_NAME="${DISPLAY_NAME:?deploy.env did not set DISPLAY_NAME for ENV_NAME=${ENV_NAME}}"
GCLOUD="${GCLOUD:?deploy.env did not set GCLOUD (gcloud CLI not found on PATH?)}"
IMAGE="us-central1-docker.pkg.dev/${PROJECT}/dispatch-console/dispatch-console:latest"
GIT_SHA="$(git -C "${REPO_ROOT}" rev-parse --short HEAD)"
# VERSION (PR-V 2026-06-03) — dispatcher-facing semantic version read from
# the repo-root VERSION file. tr strips trailing newline + any whitespace.
# Fails fast with a clear message if the file is missing (so a fresh clone
# or a corrupted working tree surfaces immediately).
VERSION="$(tr -d '[:space:]' < "${REPO_ROOT}/VERSION" 2>/dev/null || true)"
if [ -z "${VERSION}" ]; then
  echo "  ERROR: ${REPO_ROOT}/VERSION is empty or missing." >&2
  echo "  Expected a single line like '1.8.0'. See CLAUDE.md" >&2
  exit 1
fi

echo "============================================================"
echo "  ${DISPLAY_NAME} Deploy"
echo "  Project : ${PROJECT}"
echo "  Version : ${VERSION}"
echo "  Phase   : ${PHASE}"
echo "  SHA     : ${GIT_SHA}"
echo "  Image   : ${IMAGE}"
echo "  Repo    : ${REPO_ROOT}"
echo "============================================================"
echo ""

# Step 0 — Run regression tests before touching Docker
# Catches pipeline logic regressions (staging tier, school/college exclusion,
# agency normalization) before a broken build reaches Cloud Run.
echo "--> Step 0/4: Running regression tests..."
if ! python3 -c "import pytest" 2>/dev/null; then
  echo ""
  echo "  ERROR: pytest not found in the active Python environment."
  echo "  If a virtual environment is active, either install pytest in it:"
  echo "    pip install pytest"
  echo "  or deactivate it first:"
  echo "    deactivate"
  echo ""
  exit 1
fi
python3 -m pytest -q "${REPO_ROOT}/backend" || { echo ""; echo "  Tests failed — aborting build."; echo ""; exit 1; }
echo ""

# Step 1 — Build the Docker image
echo "--> Step 1/4: Building Docker image..."
docker build --platform linux/amd64 -t "${IMAGE}" \
  --build-arg GIT_SHA="${GIT_SHA}" \
  --build-arg PHASE="${PHASE}" \
  --build-arg VERSION="${VERSION}" \
  --build-arg OAUTH_CLIENT_ID="${OAUTH_CLIENT_ID}" \
  --build-arg ENV_LABEL="${ENV_LABEL}" \
  -f "${REPO_ROOT}/backend/Dockerfile" \
  "${REPO_ROOT}"

# Step 2 — Push to Artifact Registry
echo "--> Step 2/4: Pushing image to Artifact Registry..."
PATH="$(dirname "${GCLOUD}"):$PATH" docker push "${IMAGE}"

# Step 3 — Deploy to Cloud Run
echo "--> Step 3/4: Deploying to Cloud Run..."
"${GCLOUD}" run deploy dispatch-console \
  --image "${IMAGE}" \
  --region us-central1 \
  --project "${PROJECT}" \
  --quiet

# Step 4 — Prune old revisions
# Cloud Run has no native max-revisions policy; must prune manually.
# NOTE: gcloud run revisions delete only accepts one name at a time —
# xargs does NOT work here. The while-read loop is intentional.
echo "--> Step 4/4: Pruning old revisions (keeping 10 most recent)..."
"${GCLOUD}" run revisions list \
  --service dispatch-console \
  --region us-central1 \
  --project "${PROJECT}" \
  --format="value(name)" \
  | tail -n +11 \
  | while IFS= read -r rev; do
      echo "    Deleting revision: ${rev}"
      "${GCLOUD}" run revisions delete "${rev}" \
        --region us-central1 \
        --project "${PROJECT}" \
        --quiet
    done

echo ""
echo "============================================================"
echo "  Deploy complete. Verify:"
echo ""
echo "  Version endpoint:"
echo "    curl -s ${SERVICE_URL}/version | jq ."
echo "  Expected: version=${VERSION} phase=${PHASE} sha=${GIT_SHA}"
echo ""
echo "  Health check:"
echo "    curl -s ${SERVICE_URL}/health"
echo "  Expected: {\"status\": \"ok\"}"
echo "============================================================"
