# Plataforma de la nube: red (parte 2), EKS (parte 3) y RDS (parte 4).
# Todo en UNA carpeta: cada sesión es un "apply" al empezar y un "destroy"
# al terminar, sin tener que recordar en qué orden crear o borrar cosas.

terraform {
  required_version = "~> 1.17"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.73"
    }
  }

  # El ESTADO ("cuaderno" de lo que existe) vive en el bucket de la parte 1,
  # no en el PC: si el PC se pierde, Terraform sigue sabiendo qué borrar.
  # El nombre del bucket NO se escribe aquí (lleva el ID de la cuenta): se
  # pasa una sola vez al hacer "terraform init" (ver el comando en CLAUDE.md).
  backend "s3" {
    key          = "platform/terraform.tfstate"
    region       = "us-east-2"
    profile      = "deadcode"
    encrypt      = true
    use_lockfile = true # bloqueo: dos "apply" a la vez no pueden pisarse
  }
}

provider "aws" {
  region  = "us-east-2"
  profile = "deadcode"

  default_tags {
    tags = {
      Project   = "deadcode-service"
      ManagedBy = "terraform"
      Component = "platform"
    }
  }
}
