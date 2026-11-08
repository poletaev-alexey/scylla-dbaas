# Per-Cluster stack: 3 Data Nodes + 1 Monitor Node (GLOSSARY "Cluster").
#
# Node identity lives in a pre-created network interface (ENI) + Elastic IP per node, not in
# the instance. A Node Replacement (`apply -replace='aws_instance.data["data-2"]'`) therefore
# brings up a VM with the same private and public IP, so:
#   * seed lists, Prometheus targets and the Loki address on other nodes never go stale;
#   * the seed IPs are known before any instance exists (no instance-to-instance dependency);
#   * Scylla Manager keeps addressing the node by the same IP.
# See infra/README.md "Seeds and Node Replacement".

locals {
  # Data Node name -> AZ index (spread over 3 AZs). Monitor Node lives in the first AZ.
  data_nodes = { "data-1" = 0, "data-2" = 1, "data-3" = 2 }
  all_nodes  = merge(local.data_nodes, { "monitor" = 0 })

  subnet_ids = [for az in sort(keys(local.subnet_by_az)) : local.subnet_by_az[az]]

  data_ips = { for n, _ in local.data_nodes : n => aws_network_interface.node[n].private_ip }
  # Only data-1 on first creation. With Raft topology a starting node waits to reach every
  # listed seed before group 0 exists; data-2 starts only after data-1 serves CQL, so
  # listing both deadlocked the first AWS run (data-1 stuck in STARTING).
  seed_names = ["data-1"]
  monitor_ip = aws_network_interface.node["monitor"].private_ip

  secret_name = "dbaas/${var.cluster_id}/cql"
  cql_user    = "scylla"

  # Ec2Snitch: dc = region minus a trailing "-1" ("eu-central"; "us-east-2" stays), rack = AZ letter.
  scylla_dc = replace(var.region, "/-1$/", "")
}

# --- Lookups into the base stack ------------------------------------------------------------

data "aws_subnets" "public" {
  filter {
    name   = "tag:dbaas:base"
    values = [var.name_prefix]
  }
  filter {
    name   = "tag:dbaas:tier"
    values = ["public"]
  }
}

data "aws_subnet" "public" {
  for_each = toset(data.aws_subnets.public.ids)
  id       = each.value
}

locals {
  subnet_by_az = { for s in data.aws_subnet.public : s.availability_zone => s.id }
  vpc_id       = one(distinct([for s in data.aws_subnet.public : s.vpc_id]))
}

data "aws_iam_instance_profile" "node" {
  name = "${var.name_prefix}-node"
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}

# --- Security Group (ADR-0004) --------------------------------------------------------------

resource "aws_security_group" "cluster" {
  name        = "dbaas-${var.cluster_id}"
  description = "Scylla DBaaS Cluster ${var.cluster_id}"
  vpc_id      = local.vpc_id
  tags        = { Name = "dbaas-${var.cluster_id}" }
}

resource "aws_vpc_security_group_ingress_rule" "operator" {
  for_each = {
    cql        = 9042
    scylla_api = 10000
    grafana    = 3000
    manager    = 5080
    prometheus = 9090
  }
  security_group_id = aws_security_group.cluster.id
  description       = "${each.key} from operator"
  cidr_ipv4         = var.operator_cidr
  ip_protocol       = "tcp"
  from_port         = each.value
  to_port           = each.value
}

resource "aws_vpc_security_group_ingress_rule" "intra_cluster" {
  security_group_id            = aws_security_group.cluster.id
  description                  = "all traffic between nodes of this Cluster"
  referenced_security_group_id = aws_security_group.cluster.id
  ip_protocol                  = "-1"
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.cluster.id
  description       = "package repos, S3, Secrets Manager, SSM"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# --- Stable node identity: ENI + Elastic IP per node -------------------------------------

resource "aws_network_interface" "node" {
  for_each        = local.all_nodes
  subnet_id       = local.subnet_ids[each.value]
  security_groups = [aws_security_group.cluster.id]
  description     = "dbaas ${var.cluster_id} ${each.key}"
  tags            = { Name = "dbaas-${var.cluster_id}-${each.key}", "dbaas:node" = each.key }
}

resource "aws_eip" "node" {
  for_each          = local.all_nodes
  domain            = "vpc"
  network_interface = aws_network_interface.node[each.key].id
  tags              = { Name = "dbaas-${var.cluster_id}-${each.key}", "dbaas:node" = each.key }
}

# --- Credentials ----------------------------------------------------------------------------

resource "random_password" "cql" {
  length  = 32
  special = false
}

resource "random_password" "agent_token" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "cql" {
  name                    = local.secret_name
  description             = "CQL superuser and Scylla Manager Agent auth token for Cluster ${var.cluster_id}"
  recovery_window_in_days = 0 # allow re-creating a Cluster with the same id right away
}

resource "aws_secretsmanager_secret_version" "cql" {
  secret_id = aws_secretsmanager_secret.cql.id
  secret_string = jsonencode({
    username         = local.cql_user
    password         = random_password.cql.result
    agent_auth_token = random_password.agent_token.result
  })
}

# --- Data Nodes -----------------------------------------------------------------------------

resource "aws_instance" "data" {
  for_each = local.data_nodes

  ami                  = data.aws_ami.ubuntu.id
  instance_type        = var.data_instance_type
  iam_instance_profile = data.aws_iam_instance_profile.node.name

  network_interface {
    network_interface_id = aws_network_interface.node[each.key].id
    device_index         = 0
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 30
    encrypted   = true
  }

  metadata_options {
    http_tokens = "required"
  }

  user_data = templatefile("${path.module}/../cloud-init/data-node.sh.tftpl", {
    node_name           = each.key
    cluster_id          = var.cluster_id
    region              = var.region
    secret_id           = aws_secretsmanager_secret.cql.arn
    scylla_version      = var.scylla_version
    manager_version     = var.scylla_manager_version
    scylla_series       = join(".", slice(split(".", var.scylla_version), 0, 2))
    manager_series      = join(".", slice(split(".", var.scylla_manager_version), 0, 2))
    private_ip          = local.data_ips[each.key]
    public_ip           = aws_eip.node[each.key].public_ip
    monitor_ip          = local.monitor_ip
    cql_user            = local.cql_user
    replace_host_id     = lookup(var.replace_node_host_ids, each.key, "")
    bootstrap_superuser = each.key == "data-1" && !contains(keys(var.replace_node_host_ids), each.key)
    # A replacing node never lists itself as a seed (it would otherwise be allowed to
    # form a brand-new single-node cluster); on first creation the seeds are data-1/data-2.
    seeds = join(",", contains(keys(var.replace_node_host_ids), each.key)
      ? [for n, ip in local.data_ips : ip if n != each.key]
    : [for n in local.seed_names : local.data_ips[n]])
    # On first creation nodes join one at a time: data-2 waits for data-1, data-3 for both.
    # A replacing node does not wait (its peers are already up).
    wait_for_ips = contains(keys(var.replace_node_host_ids), each.key) ? [] : [
      for n, i in local.data_nodes : local.data_ips[n] if i < each.value
    ]
  })

  tags = {
    Name         = "dbaas-${var.cluster_id}-${each.key}"
    "dbaas:node" = each.key
    "dbaas:role" = "data"
  }

  lifecycle {
    # A newer Ubuntu AMI or a re-rendered user_data (e.g. replace_node_host_ids changing)
    # must never cause an in-place stop/start or replacement of healthy nodes. Only an
    # explicit `-replace` (Node Replacement) recreates a Data Node, and it uses the
    # current values of both.
    ignore_changes = [ami, user_data]
  }

  depends_on = [aws_secretsmanager_secret_version.cql]
}

# --- Monitor Node ---------------------------------------------------------------------------

resource "aws_instance" "monitor" {
  # Associate the EIP while the ENI is still unattached. Without this the association
  # raced the instance launch: "IncorrectInstanceState ... pending-instance-creation".
  # (Data Nodes already wait for it: their user_data reads the EIP's public_ip.)
  depends_on = [aws_eip.node]

  ami                  = data.aws_ami.ubuntu.id
  instance_type        = var.monitor_instance_type
  iam_instance_profile = data.aws_iam_instance_profile.node.name

  network_interface {
    network_interface_id = aws_network_interface.node["monitor"].id
    device_index         = 0
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 50
    encrypted   = true
  }

  metadata_options {
    http_tokens = "required"
  }

  user_data = templatefile("${path.module}/../cloud-init/monitor-node.sh.tftpl", {
    cluster_id         = var.cluster_id
    cluster_name       = var.cluster_name
    scylla_dc          = local.scylla_dc
    scylla_version     = var.scylla_version
    manager_version    = var.scylla_manager_version
    monitoring_version = var.scylla_monitoring_version
    monitor_ip         = local.monitor_ip
    data_ips           = [for n in sort(keys(local.data_nodes)) : local.data_ips[n]]
  })

  tags = {
    Name         = "dbaas-${var.cluster_id}-monitor"
    "dbaas:node" = "monitor"
    "dbaas:role" = "monitor"
  }

  lifecycle {
    ignore_changes = [ami, user_data]
  }
}
