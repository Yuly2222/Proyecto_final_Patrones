# Raíz de la infraestructura por ambiente. El ambiente NO es una variable: es el Workspace.
# La "nube" es Floci (emulador de AWS) corriendo en Kubernetes: http://localhost:30566
#   terraform init -backend-config=floci.s3.tfbackend
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

# Provider oficial de AWS (el mismo código serviría contra AWS real) apuntando a Floci.
provider "aws" {
  region                      = var.region
  access_key                  = "test" # Floci acepta cualquier llave: no hay secretos reales que proteger
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  skip_requesting_account_id  = true
  s3_use_path_style           = true

  endpoints {
    apigateway = var.aws_endpoint
    cloudwatch = var.aws_endpoint
    dynamodb   = var.aws_endpoint
    ec2        = var.aws_endpoint
    iam        = var.aws_endpoint
    logs       = var.aws_endpoint
    s3         = var.aws_endpoint
    sts        = var.aws_endpoint
  }

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
  source = "./modules/network"
  name   = local.name
  cidr   = lookup(local.cidr, local.env, "10.0.0.0/16") # workspace inválido: lo frena workspace_guard con su mensaje
  region = var.region
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
  vpce_id     = module.network.execute_api_vpce_id
  backend_url = "http://gateway.${local.name}.svc.cluster.local:8000" # Service del gateway en el namespace del ambiente
}
