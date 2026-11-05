# Infrastructure (`aws` Provider)

Two Terraform stacks plus cloud-init templates (ADR-0002). The Control Plane only ever runs
the `cluster` stack, through `dbaas.providers.aws.AwsInfrastructure`.

| Path | What | State |
|---|---|---|
| `base/` | VPC (one public subnet per AZ, 3 AZs), IGW, backup bucket, per-Cluster state bucket, node IAM role + instance profile | local, applied once by the operator |
| `cluster/` | One Cluster: Security Group, 4 ENIs + Elastic IPs, 3 Data Nodes (`i4i.large`), 1 Monitor Node (`t3.xlarge`), Secrets Manager secret `dbaas/<id>/cql` | S3, key `clusters/<id>.tfstate` |
| `cloud-init/` | `data-node.sh.tftpl`, `monitor-node.sh.tftpl` (user_data scripts) | – |

Pinned versions (verified November 2026): ScyllaDB **2026.3.4** (2026.4 is still RC), Scylla
Manager **3.13.0**, Scylla Monitoring **4.16.3**, Ubuntu 24.04 (Canonical, owner `099720109477`),
AWS provider `~> 5.0` (locked 5.100.0).

## Operator workflow

```bash
cd infra/base
terraform init && terraform apply                    # once; costs only S3 + nothing idle
terraform output                                     # -> backup_bucket, terraform_state_bucket

# Control Plane settings (control-plane/.env)
DBAAS_PROVIDER=aws
DBAAS_AWS_REGION=eu-central-1
DBAAS_OPERATOR_CIDR=<your public IP>/32
DBAAS_TERRAFORM_DIR=../infra/cluster
DBAAS_TERRAFORM_STATE_BUCKET=<terraform_state_bucket>
DBAAS_BACKUP_BUCKET=<backup_bucket>
DBAAS_AWS_WORK_ROOT=./.terraform-work                # one sub-dir per Cluster
DBAAS_AWS_NAME_PREFIX=dbaas                          # must match base name_prefix
```

What the driver runs per Cluster (all with `TF_DATA_DIR=<work_root>/<id>/.terraform` and
`-var-file=<work_root>/<id>/cluster.tfvars.json`):

| Operation | Terraform |
|---|---|
| create | `init -backend-config=bucket=… -backend-config=key=clusters/<id>.tfstate -backend-config=region=…`, `apply` |
| Node Replacement | `apply -replace='aws_instance.data["data-2"]'` with `replace_node_host_ids = {"data-2" = "<host id>"}` |
| Monitor Node recreation | `apply -replace=aws_instance.monitor` |
| delete | `destroy` (Backups in S3 are kept, ADR-0005) |

Manual equivalent for debugging:

```bash
cd infra/cluster
terraform init -backend-config=bucket=$STATE_BUCKET -backend-config=key=clusters/c-1234.tfstate -backend-config=region=eu-central-1
terraform plan -var cluster_id=c-1234 -var cluster_name=demo -var operator_cidr=$(curl -s ifconfig.me)/32 -var backup_bucket=$BACKUP_BUCKET
```

Shell access is SSM Session Manager only (`aws ssm start-session --target i-…`); bootstrap
logs are in `/var/log/dbaas-bootstrap.log` and `/var/log/cloud-init-output.log`.

## Seeds and Node Replacement

The failure we must avoid: a fresh VM that replaces a seed node sees only itself in its seed
list, finds no peers and bootstraps a **new single-node cluster**. How it is handled:

1. **Node identity lives in an ENI + Elastic IP, not in the instance.** Each node's network
   interface is a separate `aws_network_interface` resource; the instance attaches it as
   device 0. `apply -replace` destroys only the instance, so the replacement VM comes back
   with the *same private and public IP*. Consequences: seed lists on other nodes, Prometheus
   targets, the Loki address and the hosts Scylla Manager knows never go stale, and seed IPs
   are known before any instance exists (no instance-to-instance dependency, which `for_each`
   could not express anyway).
2. **Seeds on first creation** are the ENI IPs of `data-1` and `data-2`. Nodes join one at a
   time: `data-2` waits for `data-1` to serve CQL, `data-3` waits for both.
3. **A replacing node never lists itself as a seed.** When `replace_node_host_ids` names a
   node, its user_data gets seeds = the two *other* Data Nodes and
   `replace_node_first_boot: <host id>` (the only supported replace option; the
   `replace_address*` options are obsolete). It does not wait for peers.
4. **user_data and AMI are in `ignore_changes`.** Re-rendering user_data (because the replace
   map changed, or a newer Ubuntu AMI was published) never stops/starts or replaces a healthy
   node; only an explicit `-replace` recreates one, and it uses the current values.
5. **Same patch release.** The replace procedure requires the replacing node to run the same
   ScyllaDB patch release as the Cluster, so `scylla_version` is an exact version and the
   packages are installed with `=<version>` and held.

The dead node's Host ID is read by the driver from a live peer's Scylla REST API
(`GET :10000/storage_service/host_id`, matched on the stable private IP).

## Other notes / known limitations

* **Elastic IP quota:** 4 EIPs per Cluster; the default regional quota is 5, so request an
  increase (Service Quotas `L-0263D0A3`) before running more than one Cluster.
* **Ec2Snitch** names the DC `eu-central` (region minus `-1`) and the rack after the AZ
  (`1a`, `1b`, `1c`). Keyspaces must use `NetworkTopologyStrategy {'eu-central': 3}`.
  Prometheus targets carry `dc: eu-central`; their `cluster` label is the Cluster name, which
  must equal the name used when registering the Cluster in Scylla Manager.
* **Auth:** ScyllaDB has no default superuser. `data-1` creates `scylla` (password from the
  Cluster secret) once over the local maintenance socket (`maintenance_socket: workdir`).
* **Secret `dbaas/<id>/cql`** is JSON `{username, password, agent_auth_token}`. Scylla Manager
  needs `agent_auth_token` when the Cluster is registered.
* **Backup location:** Scylla Manager accepts only `s3:<bucket>` (no path) and writes to
  `backup/{meta,schema,sst}/cluster/<manager cluster id>/…`. The stack outputs both
  `backup_location` (`s3:<bucket>/clusters/<id>`, as stored by the Control Plane) and
  `manager_backup_location` (`s3:<bucket>`). The 30-day lifecycle rule targets `clusters/`;
  it is deliberately **not** applied to `backup/`, because Manager deduplicates SSTables
  across Backups and an age-based expiry would delete files that newer Backups still use —
  retention is Manager's job (Backup Schedule).
* **Logs:** Data Nodes forward Scylla's journal via rsyslog (`scylla_setup --rsyslog-server
  <monitor>:1514`) to the promtail syslog receiver of the Monitoring Stack, which pushes to Loki.
* The Monitor Node's Manager database lives on the Monitor Node; recreating it loses Manager
  state, so the Control Plane re-registers the Cluster afterwards. Backups in S3 are unaffected.
* Port 10000 (Scylla REST) is opened to the operator CIDR because the Control Plane uses it;
  it is unauthenticated, which is acceptable only under ADR-0004's allowlist shortcut.
