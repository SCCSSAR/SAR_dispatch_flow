FROM python:3.14.3-slim

# Git SHA injected at build time by build.sh via --build-arg GIT_SHA=$(git rev-parse --short HEAD)
# Baked into version.txt at image build so the running container always knows its own revision.
# The frontend fetches /version at page load and displays it in the footer — no hardcoded strings.
#
# VERSION (added PR-V 2026-06-03) is the dispatcher-facing semantic version
# (MAJOR.MINOR.PATCH, e.g. 1.8.0). Read from the repo-root VERSION file by
# build-*.sh and passed in here. PHASE remains the milestone codename
# (e.g. "1.8 Slacker") shown in CHANGELOG.md / GitHub Milestones. SHA is
# the per-commit fingerprint kept for debugging — surfaced via tooltip
# in the footer rather than primary display, per the dispatcher confusion
# observation that motivated the PR-V change.
ARG GIT_SHA=unknown
ARG PHASE=1.5w
ARG VERSION=unknown
# DESIGN DECISION (do not revert without team discussion): OAuth client ID MUST be passed
# as --build-arg at docker build time. Each GCP project has its own OAuth client; using the
# wrong one causes origin_mismatch on Google Sign-In. NO DEFAULT — a missing --build-arg
# fails the build loudly. (Strengthened by PR-H of the 2026-05 security review; the prior
# default silently produced misdeploys if a build script forgot to set --build-arg.)
ARG OAUTH_CLIENT_ID
RUN test -n "${OAUTH_CLIENT_ID}" || (echo "FATAL: --build-arg OAUTH_CLIENT_ID is required (each environment has its own OAuth client; see build-*.sh scripts)" >&2 && exit 1)

# Run as non-root user for security
RUN useradd -m -u 1000 appuser

WORKDIR /app

# Install dependencies
# Build context is the repo root; source files live in backend/
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY backend/*.py .

# Copy frontend (served at "/" by the FastAPI app)
COPY frontend/ ./frontend/

# Substitute environment-specific OAuth client ID into index.html.
# Each GCP project has its own OAuth client with its own registered JavaScript origins.
# Using the wrong client ID causes origin_mismatch on Google Sign-In.
RUN sed -i "s|data-client_id=\"[^\"]*\"|data-client_id=\"${OAUTH_CLIENT_ID}\"|" /app/frontend/index.html

# Sandbox banner label (#742). DELIBERATELY OPTIONAL and defaulting to empty —
# the opposite of OAUTH_CLIENT_ID's fail-loud guard above, and for the opposite
# reason. The marker belongs on the SANDBOX: an environment that forgets to pass
# ENV_LABEL renders no banner and therefore looks like the dispatcher-facing
# environment, which makes the operator MORE careful. A fail-loud guard here
# would instead force every future environment to declare itself, and a
# mis-declared one would label production as a sandbox.
ARG ENV_LABEL=""
RUN sed -i "s|data-env-label=\"[^\"]*\"|data-env-label=\"${ENV_LABEL}\"|" /app/frontend/index.html

# Write version file — read by main.py at startup and served at GET /version.
# Pipe-delimited 3-field format (PR-V 2026-06-03): VERSION | PHASE | GIT_SHA.
# The " | " separator unambiguously distinguishes the new format from the
# legacy "PHASE / GIT_SHA" format, so the main.py parser can support both
# (transition safety during a deploy in progress).
RUN echo "${VERSION} | ${PHASE} | ${GIT_SHA}" > /app/version.txt

# Switch to non-root user
USER appuser

# Cloud Run sets PORT env var; default to 8080
ENV PORT=8080

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
