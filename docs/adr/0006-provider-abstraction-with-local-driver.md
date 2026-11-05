# Provider abstraction with an `aws` and a `local` driver

The Control Plane talks to infrastructure through a Provider interface with two drivers: `aws` (Terraform + cloud-init on EC2, S3 backups) and `local` (Data and Monitor Nodes as Docker containers, MinIO instead of S3). The `local` driver costs roughly half a day but lets CI exercise the full lifecycle (create, backup, restore, Node Replacement) on every PR without cloud spend or credentials. Real AWS end-to-end runs only via a manual workflow using GitHub OIDC.
