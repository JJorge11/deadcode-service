# RED (VPC). Dibujo:
#
#   internet
#      |
#   [Internet Gateway]  <- la puerta de la VPC hacia internet
#      |
#   subredes PÚBLICAS (2 zonas)  -> NAT gateway y, más adelante, el balanceador
#      |
#   [NAT gateway]  <- deja SALIR a internet, pero nadie de afuera puede ENTRAR
#      |
#   subredes PRIVADAS (2 zonas)  -> nodos de EKS, pods y RDS (partes 3 y 4)
#
# Costo: VPC, subredes y tablas de rutas = gratis. NAT gateway ~0,045 USD/h
# + su IP pública 0,005 USD/h (+0,045 USD por GB que pase). Se destruye al
# terminar cada sesión.

# "Zonas" = centros de datos separados dentro de la región. EKS exige
# subredes en al menos 2: si un edificio falla, el otro sigue.
data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  name = "deadcode"
  azs  = slice(data.aws_availability_zones.available.names, 0, 2)

  # 10.20.x.x: no choca con la red de kind (172.18.x.x) ni con la de Docker.
  vpc_cidr = "10.20.0.0/16" # 65.536 direcciones

  # cidrsubnet(red, bits_extra, número) corta la red en pedazos:
  #   públicas: /24 = 256 direcciones (10.20.0.0/24 y 10.20.1.0/24)
  #   privadas: /20 = 4.096 direcciones (10.20.16.0/20 y 10.20.32.0/20).
  #   Grandes porque en EKS CADA POD recibe una IP de la VPC.
  public_cidrs  = [for i in range(2) : cidrsubnet(local.vpc_cidr, 8, i)]
  private_cidrs = [for i in range(2) : cidrsubnet(local.vpc_cidr, 4, i + 1)]
}

resource "aws_vpc" "main" {
  cidr_block = local.vpc_cidr

  # EKS los exige: los nodos se encuentran por nombre (DNS).
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = local.name }
}

# AWS crea un "grupo de seguridad" (firewall) por defecto que deja pasar
# TODO entre lo que lo use. Lo vaciamos: nada queda abierto por accidente.
resource "aws_default_security_group" "default" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${local.name}-default-cerrado" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = local.name }
}

# --- Subredes ---------------------------------------------------------------
# "count = 2" crea 2 copias (una por zona); count.index vale 0 y luego 1.

resource "aws_subnet" "public" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = local.public_cidrs[count.index]

  # Nada recibe IP pública automática: en lo público solo irán el NAT y el
  # balanceador, que traen la suya.
  map_public_ip_on_launch = false

  tags = {
    Name = "${local.name}-publica-${local.azs[count.index]}"
    # Etiqueta que EKS busca para poner aquí los balanceadores de internet.
    "kubernetes.io/role/elb" = "1"
  }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = local.private_cidrs[count.index]

  tags = {
    Name = "${local.name}-privada-${local.azs[count.index]}"
    # Para balanceadores INTERNOS (solo visibles dentro de la VPC).
    "kubernetes.io/role/internal-elb" = "1"
  }
}

# --- NAT gateway (salida a internet para lo privado) ------------------------
# Uno solo (no uno por zona): más barato. Si su zona cae, lo privado se queda
# sin internet un rato; aceptable para un proyecto de aprendizaje.

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${local.name}-nat" }
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  tags          = { Name = local.name }

  # El NAT necesita que la puerta a internet exista primero.
  depends_on = [aws_internet_gateway.main]
}

# --- Rutas: "para ir a X, sal por Y" ----------------------------------------
# Lo de dentro de la VPC (10.20.x.x) se encuentra solo; solo hay que decir
# por dónde sale lo demás (0.0.0.0/0 = "cualquier otra dirección").

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id # directo a internet
  }
  tags = { Name = "${local.name}-publica" }
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id # a internet, pero por el NAT
  }
  tags = { Name = "${local.name}-privada" }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}
