terraform {
  required_version = ">= 1.6"

  # One state per Cluster: the Control Plane runs
  #   terraform init -backend-config=bucket=<state bucket> \
  #                  -backend-config=key=clusters/<cluster_id>.tfstate \
  #                  -backend-config=region=<region>
  backend "s3" {}

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      "dbaas:managed_by" = "terraform"
      "dbaas:cluster_id" = var.cluster_id
    }
  }
}
