# VPC dedicada por ambiente: 2 subredes públicas + 2 privadas (2 AZ), sin NAT (el tráfico hacia
# AWS sale por VPC Endpoints, no por internet).
variable "name" {}
variable "cidr" {}
variable "region" {}

data "aws_availability_zones" "az" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.az.names, 0, 2)
}

resource "aws_vpc" "this" {
  cidr_block           = var.cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # obligatorio para private DNS de los Interface Endpoints
  tags                 = { Name = "${var.name}-vpc" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name}-igw" }
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.cidr, 8, count.index) # x.x.0.0/24, x.x.1.0/24
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.name}-public-${count.index}", "kubernetes.io/role/elb" = "1" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.this.id
  cidr_block        = cidrsubnet(var.cidr, 8, count.index + 10) # x.x.10.0/24, x.x.11.0/24
  availability_zone = local.azs[count.index]
  tags              = { Name = "${var.name}-private-${count.index}", "kubernetes.io/role/internal-elb" = "1" }
}

# Pública: 0.0.0.0/0 -> Internet Gateway
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  tags = { Name = "${var.name}-public-rt" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# Privada: SIN ruta a internet. Solo "local" (la VPC) + las rutas que inyectan los Gateway Endpoints.
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "${var.name}-private-rt" }
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# --- Gateway Endpoints (gratis): agregan a la tabla de rutas privada la prefix-list de S3/DynamoDB ---
resource "aws_vpc_endpoint" "gateway" {
  for_each          = toset(["s3", "dynamodb"])
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private.id]
  tags              = { Name = "${var.name}-vpce-${each.key}" }
}

# --- Interface Endpoints (ENI privadas en las subredes privadas): API Gateway y CloudWatch Logs ---
resource "aws_security_group" "endpoints" {
  name        = "${var.name}-vpce-sg"
  description = "HTTPS desde dentro de la VPC hacia los Interface Endpoints"
  vpc_id      = aws_vpc.this.id
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.cidr]
  }
}

resource "aws_vpc_endpoint" "interface" {
  for_each            = toset(["execute-api", "logs"])
  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true
  tags                = { Name = "${var.name}-vpce-${each.key}" }
}

# SG que usarían los nodos/pods de EKS: solo aceptan tráfico que venga desde dentro de la VPC.
resource "aws_security_group" "k8s_nodes" {
  name        = "${var.name}-k8s-nodes-sg"
  description = "Nodos de Kubernetes: entrada solo desde la VPC (NLB interno / VPC Link)"
  vpc_id      = aws_vpc.this.id
  ingress {
    from_port   = 30000
    to_port     = 32767
    protocol    = "tcp"
    cidr_blocks = [var.cidr]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

output "vpc_id" {
  value = aws_vpc.this.id
}

output "execute_api_vpce_id" {
  value = aws_vpc_endpoint.interface["execute-api"].id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "endpoint_ids" {
  value = merge(
    { for k, v in aws_vpc_endpoint.gateway : k => v.id },
    { for k, v in aws_vpc_endpoint.interface : k => v.id },
  )
}
