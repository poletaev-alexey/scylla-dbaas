variable "region" {
  type    = string
  default = "eu-central-1"
}

variable "name_prefix" {
  description = "name_prefix of the base stack; used to find its subnets and instance profile."
  type        = string
  default     = "dbaas"
}

variable "cluster_id" {
  description = "Control Plane id of the Cluster (also the Scylla cluster_name)."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,40}$", var.cluster_id))
    error_message = "cluster_id must be lowercase alphanumeric/dashes."
  }
}

variable "cluster_name" {
  description = "Human-readable Cluster name; used as the Prometheus 'cluster' label, which must match the name registered in Scylla Manager."
  type        = string
}

variable "operator_cidr" {
  description = "CIDR allowed to reach CQL, Scylla REST, Grafana, Manager API and Prometheus (ADR-0004)."
  type        = string

  validation {
    condition     = can(cidrhost(var.operator_cidr, 0))
    error_message = "operator_cidr must be a valid CIDR."
  }
}

variable "backup_bucket" {
  description = "Backup bucket created by the base stack."
  type        = string
}

variable "data_instance_type" {
  type    = string
  default = "i4i.large"
}

variable "monitor_instance_type" {
  type    = string
  default = "t3.xlarge"
}

variable "scylla_version" {
  description = "Exact ScyllaDB patch release. Pinned so a Node Replacement months later installs the same release as the rest of the Cluster (required by the replace procedure)."
  type        = string
  default     = "2026.3.4"

  validation {
    condition     = can(regex("^\\d{4}\\.\\d+\\.\\d+$", var.scylla_version))
    error_message = "scylla_version must look like 2026.3.4."
  }
}

variable "scylla_manager_version" {
  type    = string
  default = "3.13.0"
}

variable "scylla_monitoring_version" {
  type    = string
  default = "4.16.3"
}

variable "replace_node_host_ids" {
  description = <<-EOT
    Node Replacement input: Data Node name -> Scylla Host ID of the dead node it replaces,
    e.g. { "data-2" = "675ed9f4-..." }. Only affects the user_data of the named node, which
    is rendered when that instance is (re)created by `apply -replace='aws_instance.data["data-2"]'`.
  EOT
  type        = map(string)
  default     = {}

  validation {
    condition     = alltrue([for k in keys(var.replace_node_host_ids) : contains(["data-1", "data-2", "data-3"], k)])
    error_message = "replace_node_host_ids keys must be data-1, data-2 or data-3."
  }
}
