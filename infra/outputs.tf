output "environment" {
  value = terraform.workspace
}

output "vpc_id" {
  value = module.network.vpc_id
}

output "vpc_endpoints" {
  value = module.network.endpoint_ids
}

output "tables" {
  value = module.dynamodb.table_names
}

output "log_groups" {
  value = module.observability.log_groups
}

output "private_api_id" {
  value = module.apigateway.api_id
}
