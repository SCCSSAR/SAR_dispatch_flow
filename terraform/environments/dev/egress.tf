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
