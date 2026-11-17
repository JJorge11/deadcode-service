# NODOS SOLO PARA SANDBOXES (código ajeno). Diferencias con "general":
#   - taint deadcode/pool=sandbox:NoSchedule -> nada más entra aquí (la API,
#     Grafana, etc. no tienen el "pase"). Solo los pods de sandbox.py con
#     SANDBOX_NODE_POOL=sandbox (nodeSelector + toleration).
#   - podPidsLimit BAJO: un repo con una "bomba de procesos" se frena en su
#     pod. (Kubernetes solo permite este límite por NODO, por eso un grupo
#     de nodos aparte; en kind era 1024 para todo.)
#   - IMDS cerrado a los pods: no pueden pedir las credenciales del nodo.

locals {
  # Medido (pids.peak = máximo de procesos + hilos) con análisis reales en
  # Docker: Vulture 6, descarga 6, tests de itsdangerous/requests/este repo
  # 6, click 11 (sus tests lanzan subprocesos). 128 = igual que en Docker,
  # ~11x lo medido. OJO: también aplica a los DaemonSets del sistema en
  # estos nodos (aws-node, kube-proxy): medidos en el clúster (CLAUDE.md).
  sandbox_pids_limit = 128

  # Del plan gratis: 2 CPU + 4 GB, 0,085 USD/h. Cuota de sandboxes: 2 GiB.
  sandbox_instance_type = "c7i-flex.large"
}

# "Launch template" = receta de cómo arrancar cada máquina del grupo.
resource "aws_launch_template" "sandbox" {
  name_prefix = "${local.cluster_name}-sandbox-"

  # Amazon Linux 2023 se configura con "nodeadm": le pasamos un NodeConfig
  # con lo que queremos cambiar del kubelet. Va en formato MIME (como un
  # correo con adjuntos) porque EKS le agrega su propia parte (unirse al
  # clúster) y las mezcla.
  user_data = base64encode(<<-EOT
    MIME-Version: 1.0
    Content-Type: multipart/mixed; boundary="BOUNDARY"

    --BOUNDARY
    Content-Type: application/node.eks.aws

    ---
    apiVersion: node.eks.aws/v1alpha1
    kind: NodeConfig
    spec:
      kubelet:
        config:
          podPidsLimit: ${local.sandbox_pids_limit}

    --BOUNDARY--
  EOT
  )

  # Disco (con launch template se declara aquí, no en el grupo de nodos).
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 20
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  # IMDS = el servicio de la máquina (169.254.169.254) que entrega las
  # credenciales del rol del NODO. http_tokens "required" = solo IMDSv2
  # (pide un token antes). hop_limit 1 = la respuesta con el token no
  # sobrevive un "salto" de red más: llega al nodo pero NO a los pods
  # (que están un salto más allá). Así el código ajeno no puede robarlas.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${local.cluster_name}-sandbox" }
  }
}

resource "aws_eks_node_group" "sandbox" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = "sandbox"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = aws_subnet.private[*].id

  ami_type       = "AL2023_x86_64_STANDARD"
  instance_types = [local.sandbox_instance_type]
  capacity_type  = "ON_DEMAND"

  launch_template {
    id      = aws_launch_template.sandbox.id
    version = aws_launch_template.sandbox.latest_version
  }

  scaling_config {
    desired_size = 1
    min_size     = 1
    max_size     = 2
  }

  update_config {
    max_unavailable = 1
  }

  labels = {
    "deadcode/pool" = "sandbox"
  }

  # "Prohibido entrar" salvo con el pase (toleration). Los DaemonSets del
  # sistema (red, kube-proxy) toleran todo y sí corren aquí.
  taint {
    key    = "deadcode/pool"
    value  = "sandbox"
    effect = "NO_SCHEDULE"
  }

  depends_on = [
    aws_iam_role_policy_attachment.node,
    aws_eks_addon.vpc_cni,
    aws_eks_addon.kube_proxy,
    aws_route_table_association.private,
  ]
}
