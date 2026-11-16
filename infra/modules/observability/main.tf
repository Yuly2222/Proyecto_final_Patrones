# Un grupo de logs por microservicio y ambiente: /aegisvolt/<env>/<servicio>
variable "env" {}

locals {
  services = ["station-reservation-service", "billing-wallet-service", "energy-dispatch-service", "graphql-gateway"]
}

resource "aws_cloudwatch_log_group" "svc" {
  for_each          = toset(local.services)
  name              = "/aegisvolt/${var.env}/${each.key}"
  retention_in_days = var.env == "prod" ? 30 : 7
}

# Métrica de errores por ambiente: la usa el canary para decidir promover o hacer rollback.
resource "aws_cloudwatch_log_metric_filter" "errors" {
  for_each       = aws_cloudwatch_log_group.svc
  name           = "errors-${each.key}"
  log_group_name = each.value.name
  pattern        = "{ $.level = \"ERROR\" }"
  metric_transformation {
    name      = "Errors"
    namespace = "AegisVolt/${var.env}"
    value     = "1"
    dimensions = { Service = "$.service" }
  }
}

# Métrica de negocio: sagas revertidas.
resource "aws_cloudwatch_log_metric_filter" "rollbacks" {
  name           = "saga-rollbacks"
  log_group_name = aws_cloudwatch_log_group.svc["graphql-gateway"].name
  pattern        = "{ $.event = \"saga_rolled_back\" }"
  metric_transformation {
    name      = "SagaRollbacks"
    namespace = "AegisVolt/${var.env}"
    value     = "1"
  }
}

output "log_groups" {
  value = [for g in aws_cloudwatch_log_group.svc : g.name]
}
