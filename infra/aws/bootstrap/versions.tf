# Versiones fijas: Terraform y el "proveedor" de AWS (el traductor entre
# Terraform y la API de AWS). Igual que con uv.lock, fijar versiones hace
# que hoy y dentro de 6 meses se cree exactamente lo mismo.
# El archivo .terraform.lock.hcl (generado por "terraform init") guarda la
# versión EXACTA y su huella digital: va a git.

terraform {
  required_version = "~> 1.17" # 1.17.x o superior, pero no 2.0

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.73" # 6.73 o superior, pero no 7.0 (podría romper cosas)
    }
  }

  # Este bootstrap guarda su estado EN EL PC (terraform.tfstate, ignorado
  # por git): no puede guardarlo en el bucket que él mismo crea (sería el
  # huevo y la gallina). Las demás partes sí usarán este bucket.
}

provider "aws" {
  region  = "us-east-2" # la región de nuestro proyecto (fija en la nueva experiencia de AWS)
  profile = "deadcode"  # el perfil de "aws login": credenciales temporales, sin claves en archivos

  # Etiquetas que se ponen SOLAS a todo lo que cree Terraform. Sirven para
  # saber de dónde salió cada cosa y filtrar los costos por proyecto.
  default_tags {
    tags = {
      Project   = "deadcode-service"
      ManagedBy = "terraform"
      Component = "bootstrap"
    }
  }
}
