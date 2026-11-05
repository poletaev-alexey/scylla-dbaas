# Clusters in a public subnet, restricted to the operator's IP

Because the Control Plane and the CQL client run on the operator's laptop (ADR-0001), Data and Monitor Nodes get public IPs, and Security Groups allow CQL (9042), Scylla REST (10000), Grafana (3000) and Manager API (5080) only from an allowlisted operator CIDR. Shell access uses SSM Session Manager, not SSH keys. This is a known shortcut: production would use private subnets, PrivateLink/VPN, and client-to-node TLS.
