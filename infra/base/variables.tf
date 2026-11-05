variable "region" {
  description = "AWS region for the base stack and every Cluster."
  type        = string
  default     = "eu-central-1"
}

variable "name_prefix" {
  description = "Prefix for every base resource; the cluster module looks resources up by it."
  type        = string
  default     = "dbaas"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.name_prefix))
    error_message = "name_prefix must be lowercase alphanumeric/dashes (it is part of S3 bucket names)."
  }
}

variable "vpc_cidr" {
  description = "CIDR of the shared VPC; each AZ gets a /20 public subnet."
  type        = string
  default     = "10.42.0.0/16"
}

variable "backup_expiration_days" {
  description = "Lifecycle expiration for objects under clusters/ in the backup bucket."
  type        = number
  default     = 30
}
