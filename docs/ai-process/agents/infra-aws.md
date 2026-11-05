# Agent: infra-aws

Goal: everything needed for the `aws` Provider driver except Scylla/Manager API calls.

Own: `infra/` (all of it), `control-plane/src/dbaas/providers/aws.py`,
`control-plane/tests/test_aws_provider.py`. May add `boto3` usage (already an optional dep).

1. `infra/base` (Terraform ≥1.6, AWS provider ~>5): VPC with one public subnet per AZ (3 AZs
   in eu-central-1), IGW, S3 backup bucket (SSE-S3, block public access, 30-day lifecycle on
   `clusters/`), S3 bucket for per-Cluster Terraform state, IAM role + instance profile for
   nodes (S3 read/write on the backup bucket, SSM managed instance core, Secrets Manager read
   of `dbaas/*`), outputs consumed by the cluster module. Variables: `region`, `name_prefix`.
2. `infra/cluster` module (one state per Cluster, backend key `clusters/<id>.tfstate`,
   configured with `-backend-config`): Security Group (9042, 10000, 3000, 5080, 9090 from
   `operator_cidr`; all traffic inside the SG), 3 Data Nodes `i4i.large` spread across AZs,
   1 Monitor Node `t3.xlarge`, latest Ubuntu 24.04 AMI via data source (Canonical owner
   099720109477), Secrets Manager secret `dbaas/<cluster_id>/cql` with a random password,
   tags `dbaas:cluster_id`, `dbaas:node`. Use `for_each` keyed by node name (`data-1`…) so
   `terraform apply -replace='aws_instance.data["data-2"]'` is a Node Replacement.
   Outputs: JSON-friendly map of nodes (name, role, instance_id, public_ip, private_ip),
   grafana_url, manager_url, secret_arn, backup_location (`s3:<bucket>/clusters/<id>`).
3. `infra/cloud-init/`: templates for Data Node (ScyllaDB latest stable from the official
   repo for Ubuntu 24.04, `scylla_setup` non-interactive incl. NVMe RAID/XFS, scylla.yaml with
   cluster_name, seeds = private IPs of data-1/data-2, PasswordAuthenticator +
   CassandraAuthorizer, Ec2Snitch, and `replace_node_first_boot` support driven by a template
   variable; scylla-manager-agent with auth token and S3 config) and Monitor Node (Docker,
   scylla-manager + its backend, scylla-monitoring stack incl. Loki, Prometheus targets =
   Data Node private IPs). Data Nodes ship journald to the Monitor Node's Loki (promtail).
   Seeds must not depend on instances being replaced (avoid a replaced seed bootstrapping a
   new cluster) — document how you handled it.
4. `providers/aws.py`: `class AwsInfrastructure` implementing `Infrastructure` by running
   terraform as an async subprocess (`asyncio.create_subprocess_exec`), streaming each
   output line to `StepLog` (throttled), one working dir per Cluster under a configurable
   path, `describe()` via boto3 `describe_instances` filtered by tag, `get_credentials()`
   via Secrets Manager. Expose `build(settings) -> Provider` combining it with
   `dbaas.scylla.ops.ManagerClusterOps` (being written by another agent; import lazily;
   if absent at test time, tests use the fake ops).
5. Tests: unit tests with moto for describe/get_credentials; terraform invocation tested
   with a fake terraform binary script on PATH. Run `terraform fmt -check -recursive` and
   `terraform validate` (with `init -backend=false`) — terraform is installed.
