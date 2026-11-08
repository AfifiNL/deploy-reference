terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.72"
    }
  }

  # State: configure a remote backend (S3 with locking) before a shared or real apply.
  # Left unset here on purpose: this configuration has only been validated, never applied.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = merge(var.tags, {
      project    = var.name
      managed-by = "terraform"
    })
  }
}
