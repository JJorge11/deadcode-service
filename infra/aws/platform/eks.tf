# CLÚSTER DE KUBERNETES (EKS). Piezas:
#   - "plano de control" (control plane): el cerebro (API de K8s, etcd).
#     Lo administra AWS; cobra 0,10 USD/h en soporte ESTÁNDAR.
#   - "grupo de nodos": las máquinas EC2 donde corren los pods.
#   - "add-ons": piezas de K8s que AWS instala y actualiza (red, DNS...).
#   - roles IAM: permisos que AWS presta al clúster y a los nodos.

locals {
  cluster_name = "deadcode"
  # 1.37 = misma versión que el clúster kind. Comprobar SIEMPRE que siga en
  # soporte estándar: aws eks describe-cluster-versions --profile deadcode
  cluster_version = "1.37"

  # Tipo de máquina de los nodos: del plan gratis (free-tier-eligible),
  # 2 CPU + 8 GB, 0,096 USD/h cada uno.
  node_instance_type = "m7i-flex.large"
}

# --- Tu IP pública ------------------------------------------------------------
# La API del clúster solo aceptará conexiones desde TU IP (además de pedir
# credenciales de AWS). Se pregunta en cada "plan": si tu IP cambia, se
# actualiza sola en el siguiente apply.
data "http" "my_ip" {
  url = "https://checkip.amazonaws.com"
}

# --- Roles IAM ------------------------------------------------------------------
# Un "rol" = un gafete con permisos. "assume_role_policy" dice QUIÉN puede
# ponérselo (aquí: el servicio EKS o las máquinas EC2), y las "policy
# attachments" dicen QUÉ puede hacer. Las políticas son las oficiales de AWS.

data "aws_iam_policy_document" "eks_assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster" {
  name               = "${local.cluster_name}-eks-cluster"
  assume_role_policy = data.aws_iam_policy_document.eks_assume.json
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name               = "${local.cluster_name}-eks-node"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

# Lo mínimo para un nodo: unirse al clúster, dar IPs a los pods (CNI) y
# descargar las imágenes de los add-ons (vienen del registro ECR de AWS).
resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
  ])
  role       = aws_iam_role.node.name
  policy_arn = each.value
}

# --- El clúster -------------------------------------------------------------------

resource "aws_eks_cluster" "main" {
  name     = local.cluster_name
  version  = local.cluster_version
  role_arn = aws_iam_role.cluster.arn

  # No instalar las versiones "sueltas" de red/DNS: las instalamos abajo como
  # add-ons administrados, para poder configurarlos (NetworkPolicy).
  bootstrap_self_managed_addons = false

  # Quién entra: "access entries" (API de EKS, sin el viejo ConfigMap
  # aws-auth). Quien crea el clúster (tu rol de "aws login") queda como admin.
  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = true
  }

  # Si 1.37 sale del soporte estándar, AWS ACTUALIZA el clúster en vez de
  # pasarlo al soporte extendido (0,60 USD/h, 6 veces más caro).
  upgrade_policy {
    support_type = "STANDARD"
  }

  vpc_config {
    subnet_ids              = aws_subnet.private[*].id
    endpoint_private_access = true                                               # los nodos hablan con la API por dentro de la VPC
    endpoint_public_access  = true                                               # tú, desde tu PC...
    public_access_cidrs     = ["${trimspace(data.http.my_ip.response_body)}/32"] # ...solo desde tu IP
  }

  depends_on = [aws_iam_role_policy_attachment.cluster]
}

# --- Add-ons ------------------------------------------------------------------------

# vpc-cni: da a cada pod una IP de la VPC. En EKS las NetworkPolicy están
# APAGADAS por defecto: sin "enableNetworkPolicy" el deny-all de los
# sandboxes no haría NADA (en kind lo hacía kindnet).
resource "aws_eks_addon" "vpc_cni" {
  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = "vpc-cni"
  configuration_values        = jsonencode({ enableNetworkPolicy = "true" })
  resolve_conflicts_on_create = "OVERWRITE"
}

# kube-proxy: hace funcionar los Services (IP fija -> pods).
resource "aws_eks_addon" "kube_proxy" {
  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = "kube-proxy"
  resolve_conflicts_on_create = "OVERWRITE"
}

# coredns: el DNS interno ("postgres" -> IP del Service). Necesita nodos
# donde correr: va DESPUÉS del grupo de nodos.
resource "aws_eks_addon" "coredns" {
  cluster_name                = aws_eks_cluster.main.name
  addon_name                  = "coredns"
  resolve_conflicts_on_create = "OVERWRITE"
  depends_on                  = [aws_eks_node_group.general]
}

# --- Nodos ------------------------------------------------------------------------------

# Grupo "general": API, worker, Redis, ArgoCD, monitoreo. (Los sandboxes
# irán a un grupo aparte con límite de procesos bajo: parte 3.2.)
resource "aws_eks_node_group" "general" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "general"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = aws_subnet.private[*].id # en lo privado: sin IP pública

  ami_type       = "AL2023_x86_64_STANDARD" # Amazon Linux 2023 preparado para EKS
  instance_types = [local.node_instance_type]
  capacity_type  = "ON_DEMAND"
  disk_size      = 20 # GB

  scaling_config {
    desired_size = 2
    min_size     = 1
    max_size     = 3
  }

  update_config {
    max_unavailable = 1 # al actualizar, de a un nodo
  }

  labels = {
    "deadcode/pool" = "general"
  }

  # Antes de crear nodos: permisos, red de pods, kube-proxy y la salida a
  # internet (los nodos descargan imágenes por el NAT).
  depends_on = [
    aws_iam_role_policy_attachment.node,
    aws_eks_addon.vpc_cni,
    aws_eks_addon.kube_proxy,
    aws_route_table_association.private,
  ]
}
