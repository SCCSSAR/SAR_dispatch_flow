# ---------------------------------------------------------------------------
# Static egress (ops#875 Task 7). Direct VPC egress + Cloud NAT with ONE
# reserved address, so CalTopo and D4H stop seeing Google's shared, rotating
# egress pool (AWS WAF "Amazon IP Reputation List" blocked it for ~15 h on
# 2026-10-02).
#
# This is the sccssar.org org's address. It is NOT shared with personal-dev:
# Shared VPC host and service projects must be in the same organization, and
# personal-dev is the host for saferfuturesbydesign.com. Same resource names
# as terraform/environments/dev/egress.tf so the two read side by side and
# TestCloudRunStaticEgressWiring pins both with one assertion.
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
# addresses for 20 min after scale-down; dispatch-console caps at 3. Sized to
# match personal-dev so a later prod service project can share it.
#
# Private Google Access MUST be on, and explicitly. Cloud NAT auto-enables it,
# but only once the NAT covers the subnet; with ALL_TRAFFIC egress every call
# to Firestore, Secret Manager, Vertex and Cloud Tasks enters this subnet, and
# the lifespan awaits the Firestore warm-up before serving. With the flag set,
# Google-API traffic works from the first second of an instance's life and
# never touches the NAT.
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

# The address the vendors are told about (cloud-egress2.sccssar.org).
# Releasing it means a new address and new vendor notices, so it cannot be
# destroyed by accident. $3.65/mo in use; $7.30/mo if ever left unattached.
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
