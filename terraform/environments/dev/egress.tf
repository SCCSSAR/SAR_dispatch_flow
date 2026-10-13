# ---------------------------------------------------------------------------
# Static egress (ops#875). Direct VPC egress + Cloud NAT with ONE reserved
# address, so CalTopo and D4H stop seeing Google's shared, rotating egress pool
# (AWS WAF "Amazon IP Reputation List" blocked it for ~15 h on 2026-10-02).
#
# This project is the Shared VPC HOST for the saferfuturesbydesign.com org:
# the drone tracker projects attach as service projects and use this subnet,
# so one address serves every app in the org.
#
# A dedicated network, not `default`: nothing else lives here and there are no
# inherited default-allow-* firewall rules. Cloud Run does not accept VPC
# ingress, so no firewall rules are needed at all.
# ---------------------------------------------------------------------------

resource "google_compute_network" "egress" {
  name                    = "egress"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

# /23 = 510 usable addresses. Cloud Run takes 2x the instance count and keeps
# addresses for 20 min after scale-down; the summed caps of every service that
# will use this subnet are 3 (dispatch-console) + 50 + 50 (tracker dev/prod).
# Private Google Access is auto-enabled by Cloud NAT anyway; set explicitly so
# the intent is in Terraform, not a side effect.
resource "google_compute_subnetwork" "egress" {
  name                     = "egress-us-central1"
  region                   = var.region
  network                  = google_compute_network.egress.id
  ip_cidr_range            = "10.8.0.0/23"
  private_ip_google_access = true
}

resource "google_compute_router" "egress" {
  name    = "egress-router"
  region  = var.region
  network = google_compute_network.egress.id
}

# The address vendors will allowlist. Releasing it means a new address and a
# new vendor ticket, so it cannot be destroyed by accident. $3.65/mo in use;
# $7.30/mo if ever left unattached.
resource "google_compute_address" "egress" {
  name         = "egress-ip"
  region       = var.region
  address_type = "EXTERNAL"
  network_tier = "PREMIUM"

  lifecycle {
    prevent_destroy = true
  }
}

# MANUAL_ONLY: the gateway uses exactly this address and never auto-allocates
# another, so the address vendors hold is the only one traffic can leave from.
# ERRORS_ONLY logging surfaces port exhaustion and dropped translations without
# paying Vended Logs rates for every connection. Switch to ALL only while
# verifying a cutover, then back.
resource "google_compute_router_nat" "egress" {
  name                               = "egress-nat"
  region                             = var.region
  router                             = google_compute_router.egress.name
  nat_ip_allocate_option             = "MANUAL_ONLY"
  nat_ips                            = [google_compute_address.egress.self_link]
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

  subnetwork {
    name                    = google_compute_subnetwork.egress.id
    source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
  }

  log_config {
    enable = true
    filter = "ERRORS_ONLY"
  }
}

# ---------------------------------------------------------------------------
# Shared VPC: this project hosts the org's one egress address. The drone
# tracker projects attach as service projects and point their Cloud Run
# service at the subnet above. A service project can attach to ONE host, and
# host and service projects must be in the same organization
# (saferfuturesbydesign.com).
#
# Enabling the host needs roles/compute.xpnAdmin at the ORG, granted by hand
# (org IAM is outside the project-IAM-via-Terraform rule; recorded in ops#875).
# ---------------------------------------------------------------------------

resource "google_compute_shared_vpc_host_project" "egress" {
  project = var.project_id
}

# Kept beside the resources that consume it so the Shared VPC unit reads in
# one place.
variable "egress_service_projects" {
  description = "Project IDs of every service project that uses the shared egress subnet. Each needs Compute Network User on the subnet for its Cloud Run service agent."
  type        = set(string)
  default = [
    "skydio-tracker-dev-25092716",
    "skydio-tracker-25092708-prod",
  ]
}

# Project numbers are looked up rather than written down: the service agent's
# email is built from the number, and a hand-kept ID -> number map can drift.
data "google_project" "egress_service" {
  for_each   = var.egress_service_projects
  project_id = each.key
}

resource "google_compute_shared_vpc_service_project" "egress" {
  for_each        = var.egress_service_projects
  host_project    = google_compute_shared_vpc_host_project.egress.project
  service_project = each.key
}

# Per-subnet binding, not project-level, so a service project can use this one
# subnet and nothing else in the host.
resource "google_compute_subnetwork_iam_member" "egress_service_agents" {
  for_each   = var.egress_service_projects
  project    = var.project_id
  region     = var.region
  subnetwork = google_compute_subnetwork.egress.name
  role       = "roles/compute.networkUser"
  member     = "serviceAccount:service-${data.google_project.egress_service[each.key].number}@serverless-robot-prod.iam.gserviceaccount.com"
  depends_on = [google_compute_shared_vpc_service_project.egress]
}
