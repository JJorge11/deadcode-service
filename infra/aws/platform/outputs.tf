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

output "cluster_name" {
  description = "Nombre del clúster EKS"
  value       = aws_eks_cluster.main.name
}

output "kubeconfig_command" {
  description = "Crea un kubeconfig APARTE (no toca el de kind)"
  value       = "aws eks update-kubeconfig --name ${aws_eks_cluster.main.name} --region us-east-2 --profile deadcode --kubeconfig $HOME/.kube/eks-deadcode --alias eks-deadcode"
}

output "db_endpoint" {
  description = "Dirección de Postgres (solo accesible desde el clúster)"
  value       = aws_db_instance.main.address
}

output "db_name" {
  description = "Nombre de la base de datos de la app"
  value       = aws_db_instance.main.db_name
}

output "db_secret_arn" {
  description = "Secret de Secrets Manager con usuario y contraseña de Postgres"
  value       = aws_db_instance.main.master_user_secret[0].secret_arn
}
