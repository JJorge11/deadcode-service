# Bucket de S3 para el ESTADO de Terraform de las demás partes (red, EKS,
# RDS...). Es la única pieza PERMANENTE: lo demás se crea y se destruye en
# cada sesión, pero el "cuaderno" de lo que existe debe sobrevivir.
# Costo: S3 está en el plan gratis y el estado pesa pocos KB (~0 USD).

# "data" = PREGUNTAR algo a AWS (no crea nada). Aquí: ¿en qué cuenta estoy?
# Así el ID de la cuenta no queda escrito en el código.
data "aws_caller_identity" "current" {}

locals {
  # Los nombres de bucket son ÚNICOS EN TODO EL MUNDO (no solo en tu
  # cuenta): agregar el ID de la cuenta evita chocar con el de otra persona.
  state_bucket = "deadcode-tfstate-${data.aws_caller_identity.current.account_id}"
}

# "resource" = algo que Terraform CREA y administra.
resource "aws_s3_bucket" "state" {
  bucket = local.state_bucket

  # Protección: "terraform destroy" se NIEGA a borrar este bucket (sin él,
  # las demás partes pierden su "cuaderno"). Para borrarlo de verdad hay
  # que cambiar esto a false a propósito. (En la parte 1 se practicó el
  # destroy con false y se volvió a crear.)
  lifecycle {
    prevent_destroy = true
  }
}

# Versiones: cada cambio del estado guarda una copia. Si un día el estado se
# daña, se puede volver a una versión anterior.
resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Cifrado en el disco de AWS (con claves administradas por S3, sin costo).
resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Bloqueo TOTAL de acceso público: nadie de internet puede leerlo, aunque
# alguien se equivoque más adelante con un permiso.
resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Las versiones viejas del estado se borran a los 90 días (no se acumulan).
resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    id     = "borrar-versiones-viejas"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }
}
