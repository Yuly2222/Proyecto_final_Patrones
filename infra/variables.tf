variable "region" {
  default = "us-east-1"
}

# Floci expuesto por el NodePort del clúster (k8s/platform.yaml).
variable "aws_endpoint" {
  default = "http://localhost:30566"
}
