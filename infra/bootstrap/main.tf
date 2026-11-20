# Se ejecuta UNA sola vez, con estado local (huevo y gallina: el bucket que guarda
# los estados no puede guardar su propio estado antes de existir). Floci debe estar arriba (k8s/platform.yaml).
#   cd infra/bootstrap && terraform init && terraform apply
terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.80" }
  }
}

provider "aws" {
  region                      = var.region
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true
  s3_use_path_style           = true
  endpoints {
    s3  = var.aws_endpoint
    sts = var.aws_endpoint
  }
}

variable "region" {
  default = "us-east-1"
}

variable "aws_endpoint" {
  default = "http://localhost:30566"
}

data "aws_caller_identity" "me" {}

resource "aws_s3_bucket" "tfstate" {
  bucket = "aegisvolt-tfstate-${data.aws_caller_identity.me.account_id}" # nombre global único
  lifecycle {
    prevent_destroy = true
  }
}

# Requisito del proyecto: cada `terraform apply` crea una NUEVA versión del .tfstate -> backups históricos.
resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

output "state_bucket" {
  value = aws_s3_bucket.tfstate.bucket
}
