# BASE DE DATOS ADMINISTRADA (RDS, Postgres 17). Reemplaza al StatefulSet
# de Postgres del clúster: AWS se encarga del disco, parches y reinicios.
# Costo: db.t4g.micro 0,016 USD/h + disco 20 GB (~0,003 USD/h). Se crea y
# se destruye en cada sesión (los datos de la nube son de prueba).

locals {
  db_name = "deadcode"
  db_user = "deadcode"
}

# Grupo de subredes: en qué subredes puede vivir la base. Solo PRIVADAS
# (2 zonas, lo exige RDS aunque la base viva en una sola).
resource "aws_db_subnet_group" "main" {
  name       = local.cluster_name
  subnet_ids = aws_subnet.private[*].id
}

# Firewall de la base: SOLO el puerto 5432 y SOLO desde los nodos del
# clúster (su "security group", que EKS crea solo). Ni internet ni tu PC.
#
# Límite conocido: los nodos de sandboxes usan el MISMO security group
# que los generales, así que AWS no los distingue. Lo que impide que un
# sandbox llegue a la base es la NetworkPolicy deny-all (probada en EKS).
resource "aws_security_group" "rds" {
  name        = "${local.cluster_name}-rds"
  description = "Postgres: solo desde los nodos de EKS"
  vpc_id      = aws_vpc.main.id
  tags        = { Name = "${local.cluster_name}-rds" }
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_eks" {
  security_group_id            = aws_security_group.rds.id
  description                  = "Postgres desde los nodos de EKS"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_eks_cluster.main.vpc_config[0].cluster_security_group_id
}
# (Sin reglas de SALIDA: la base solo responde, nunca inicia conexiones.)

resource "aws_db_instance" "main" {
  identifier     = local.cluster_name
  engine         = "postgres"
  engine_version = "17" # la versión 17.x por defecto de AWS (17.9 hoy)
  instance_class = "db.t4g.micro"

  # Si un día la 17 sale del soporte estándar, NO pasar al extendido (de
  # pago, como en EKS): mejor que AWS obligue a actualizar.
  engine_lifecycle_support = "open-source-rds-extended-support-disabled"

  db_name  = local.db_name
  username = local.db_user
  # AWS genera la contraseña y la guarda en Secrets Manager (caja fuerte).
  # Así NUNCA aparece en el código ni en el estado de Terraform (el estado
  # guarda en texto plano todo lo que Terraform conoce; esta no la conoce).
  manage_master_user_password = true

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false # sin IP pública
  multi_az               = false # una sola zona: la mitad del precio

  # Se destruye en cada sesión: sin copias automáticas ni "foto" final.
  backup_retention_period = 0
  skip_final_snapshot     = true
  deletion_protection     = false
  apply_immediately       = true
}
