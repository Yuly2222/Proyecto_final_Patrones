# API Gateway REST de tipo PRIVATE: solo se puede invocar desde dentro de la VPC, a través del
# Interface Endpoint "execute-api". La resource policy rechaza cualquier llamada de otra VPC.
variable "name" {}
variable "env" {}
variable "vpc_id" {}
variable "backend_url" {}

resource "aws_api_gateway_rest_api" "this" {
  name = "${var.name}-graphql"
  endpoint_configuration {
    types = ["PRIVATE"]
  }
}

data "aws_iam_policy_document" "only_my_vpc" {
  statement {
    effect    = "Allow"
    actions   = ["execute-api:Invoke"]
    resources = ["${aws_api_gateway_rest_api.this.execution_arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }
  statement {
    effect    = "Deny"
    actions   = ["execute-api:Invoke"]
    resources = ["${aws_api_gateway_rest_api.this.execution_arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "aws:SourceVpc"
      values   = [var.vpc_id]
    }
  }
}

resource "aws_api_gateway_rest_api_policy" "this" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  policy      = data.aws_iam_policy_document.only_my_vpc.json
}

# /graphql -> proxy HTTP hacia el GraphQL Aggregation Layer del cluster
resource "aws_api_gateway_resource" "graphql" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_rest_api.this.root_resource_id
  path_part   = "graphql"
}

resource "aws_api_gateway_method" "post" {
  rest_api_id   = aws_api_gateway_rest_api.this.id
  resource_id   = aws_api_gateway_resource.graphql.id
  http_method   = "POST"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "graphql" {
  rest_api_id             = aws_api_gateway_rest_api.this.id
  resource_id             = aws_api_gateway_resource.graphql.id
  http_method             = aws_api_gateway_method.post.http_method
  type                    = "HTTP_PROXY"
  integration_http_method = "POST"
  uri                     = "${var.backend_url}/graphql"
  # En EKS real: connection_type = "VPC_LINK" + connection_id = <VPC Link al NLB interno del ingress>
}

resource "aws_api_gateway_deployment" "this" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  triggers = {
    redeploy = sha1(jsonencode([aws_api_gateway_integration.graphql, aws_api_gateway_rest_api_policy.this]))
  }
  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_api_gateway_stage" "env" {
  rest_api_id   = aws_api_gateway_rest_api.this.id
  deployment_id = aws_api_gateway_deployment.this.id
  stage_name    = var.env
}

output "api_id" {
  value = aws_api_gateway_rest_api.this.id
}
