# Read by the Control Plane with `terraform output -json`.

output "nodes" {
  description = "Node name -> {name, role, instance_id, public_ip, private_ip}."
  value = merge(
    {
      for n, inst in aws_instance.data : n => {
        name        = n
        role        = "data"
        instance_id = inst.id
        public_ip   = aws_eip.node[n].public_ip
        private_ip  = aws_network_interface.node[n].private_ip
      }
    },
    {
      monitor = {
        name        = "monitor"
        role        = "monitor"
        instance_id = aws_instance.monitor.id
        public_ip   = aws_eip.node["monitor"].public_ip
        private_ip  = aws_network_interface.node["monitor"].private_ip
      }
    },
  )
}

output "grafana_url" {
  value = "http://${aws_eip.node["monitor"].public_ip}:3000"
}

output "manager_url" {
  value = "http://${aws_eip.node["monitor"].public_ip}:5080"
}

output "secret_arn" {
  value = aws_secretsmanager_secret.cql.arn
}

output "backup_location" {
  description = "Per-Cluster Backup location as stored by the Control Plane."
  value       = "s3:${var.backup_bucket}/clusters/${var.cluster_id}"
}

output "manager_backup_location" {
  description = "Scylla Manager --location value. Manager accepts only [dc:]provider:bucket (no path) and lays objects out as backup/{meta,schema,sst}/cluster/<manager cluster id>/."
  value       = "s3:${var.backup_bucket}"
}

output "scylla_dc" {
  description = "Datacenter name assigned by Ec2Snitch; use it in NetworkTopologyStrategy."
  value       = local.scylla_dc
}
