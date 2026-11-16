variable "region" {
  default = "us-east-1"
}

# Los Interface Endpoints cuestan ~0.01 USD/hora cada uno. Apágalos (false) fuera de la demo.
variable "enable_interface_endpoints" {
  type    = bool
  default = false
}

# Destino del API Gateway privado. En EKS real sería el NLB del ingress vía VPC Link.
variable "backend_url" {
  default = "http://graphql-gateway.internal:8000"
}
