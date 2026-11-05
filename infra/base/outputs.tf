# Consumed by the cluster module (looked up by name_prefix) and by the Control Plane settings
# (DBAAS_BACKUP_BUCKET, DBAAS_TERRAFORM_STATE_BUCKET).

output "region" {
  value = var.region
}

output "name_prefix" {
  value = var.name_prefix
}

output "vpc_id" {
  value = aws_vpc.this.id
}

output "subnet_ids" {
  description = "Public subnet id per AZ."
  value       = { for az, s in aws_subnet.public : az => s.id }
}

output "backup_bucket" {
  value = aws_s3_bucket.backups.bucket
}

output "terraform_state_bucket" {
  value = aws_s3_bucket.tfstate.bucket
}

output "node_instance_profile" {
  value = aws_iam_instance_profile.node.name
}

output "node_role_arn" {
  value = aws_iam_role.node.arn
}
