# Database-per-Service: cada microservicio tiene SU tabla, y cada ambiente su copia:
#   aegisvolt-reservations-dev, aegisvolt-billing-test, aegisvolt-dispatch-prod, ...
variable "env" {}

locals {
  tables = {
    reservations = "connector_id" # station-reservation-service
    billing      = "pk"           # billing-wallet-service
    dispatch     = "session_id"   # energy-dispatch-service
    sagas        = "saga_id"      # graphql-gateway (orquestador SAGA)
    flags        = "flag"         # feature flags del ambiente
  }
  is_prod = var.env == "prod"
}

resource "aws_dynamodb_table" "this" {
  for_each     = local.tables
  name         = "aegisvolt-${each.key}-${var.env}"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = each.value

  attribute {
    name = each.value
    type = "S"
  }

  point_in_time_recovery {
    enabled = local.is_prod
  }
  deletion_protection_enabled = local.is_prod
}

output "table_names" {
  value = { for k, t in aws_dynamodb_table.this : k => t.name }
}
