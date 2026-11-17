# "output" = un dato que Terraform MUESTRA al terminar (y que otras partes
# pueden consultar). El nombre del bucket lo usaremos en la parte 2.
output "state_bucket" {
  description = "Bucket donde las demás partes guardan su estado de Terraform"
  value       = aws_s3_bucket.state.bucket
}
