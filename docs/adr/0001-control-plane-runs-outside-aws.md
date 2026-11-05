# Control Plane runs outside AWS (local docker-compose)

The Control Plane (API, UI, workers, its database) runs on the operator's machine via docker-compose rather than on a VM inside AWS. This keeps it independent of the infrastructure it creates and destroys, avoids paying for an always-on VM during a short assignment, and makes the demo reproducible from a laptop. The cost is that Clusters must be reachable from the operator's public IP (see ADR-0004); a production version would host the Control Plane in its own account/VPC with private connectivity to Clusters.
