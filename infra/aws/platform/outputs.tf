output "vpc_id" {
  description = "ID de la VPC"
  value       = aws_vpc.main.id
}

output "public_subnet_ids" {
  description = "Subredes públicas (NAT y balanceadores)"
  value       = aws_subnet.public[*].id
}

output "private_subnet_ids" {
  description = "Subredes privadas (nodos de EKS y RDS)"
  value       = aws_subnet.private[*].id
}

output "nat_public_ip" {
  description = "IP con la que lo privado sale a internet"
  value       = aws_eip.nat.public_ip
}
