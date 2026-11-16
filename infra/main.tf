# Raíz de la infraestructura por ambiente. El ambiente NO es una variable: es el Workspace.
#   terraform init -backend-config="bucket=aegisvolt-tfstate-<ACCOUNT_ID>"
#   terraform workspace select -or-create dev     (o test / prod)
#   terraform apply
terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.80" }
  }
  # Estado remoto en S3 versionado. Con workspaces, S3 guarda cada ambiente en su propia llave:
  #   env:/dev/aegisvolt/terraform.tfstate, env:/test/..., env:/prod/...
  backend "s3" {
    key          = "aegisvolt/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true # bloqueo nativo en S3 (Terraform >= 1.10): dos applies no pisan el mismo estado
  }
}

locals {
  env  = terraform.workspace
  name = "aegisvolt-${local.env}"
  cidr = { dev = "10.10.0.0/16", test = "10.20.0.0/16", prod = "10.30.0.0/16" }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = "aegisvolt", Environment = local.env, ManagedBy = "terraform" }
  }
}

# Freno de seguridad: nunca aplicar en el workspace "default" ni en uno inventado.
resource "terraform_data" "workspace_guard" {
  lifecycle {
    precondition {
      condition     = contains(["dev", "test", "prod"], terraform.workspace)
      error_message = "Selecciona un workspace válido: terraform workspace select dev|test|prod"
    }
  }
}

module "network" {
  source                     = "./modules/network"
  name                       = local.name
  cidr                       = local.cidr[local.env]
  region                     = var.region
  enable_interface_endpoints = var.enable_interface_endpoints
}

module "dynamodb" {
  source = "./modules/dynamodb"
  env    = local.env
}

module "observability" {
  source = "./modules/observability"
  env    = local.env
}

module "apigateway" {
  source      = "./modules/apigateway"
  name        = local.name
  env         = local.env
  vpc_id      = module.network.vpc_id
  backend_url = var.backend_url
}
