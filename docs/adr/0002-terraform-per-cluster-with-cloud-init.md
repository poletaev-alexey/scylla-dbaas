# Terraform state per Cluster, nodes configured by cloud-init

Infrastructure is split into a long-lived `base` stack (VPC, backup bucket, IAM) and one Terraform state per Cluster (S3 backend, keyed by cluster id) that the Control Plane drives by invoking `terraform` as a subprocess. Node replacement is `apply -replace=<node>`. VMs configure themselves via cloud-init (Scylla repo, packages, `scylla_setup`, seeds, manager-agent), so the Control Plane never needs SSH.

## Considered Options

- boto3 only: finer control, but no declarative IaC to show, validate or review in CI.
- Ansible (scylla-ansible-roles): mature, but requires SSH reachability and inventory management from the Control Plane.
- Packer-baked AMI: faster, immutable boot; deferred as a production improvement because image builds slow down iteration.
