"""Ejecuta el análisis en un contenedor desechable (sandbox).

El worker NO analiza el código ajeno en su propio contenedor (que tiene
la contraseña de Postgres y acceso a Redis). En su lugar crea, para
cada análisis, un contenedor nuevo:

  - sin red (--network none): no ve Postgres, Redis ni internet
  - sin variables de entorno: no recibe ninguna contraseña
  - disco de solo lectura; solo /tmp en memoria, sin poder ejecutar ahí
  - usuario "nobody", sin privilegios de Linux (--cap-drop ALL)
  - límites de memoria, CPU, procesos y tiempo
  - se destruye entero al terminar (--rm)

El worker le pasa el .tar.gz por stdin y recibe el JSON por stdout.

Modos (variable SANDBOX_MODE):
  - "docker": lo anterior. Es el modo de Docker Compose.
  - "local":  analiza en el mismo proceso, SIN aislamiento. Solo para
              tests y desarrollo en Windows (donde no hay sandbox).
"""

import json
import logging
import os
import subprocess
import uuid

from deadcode_service import sandbox_main
from deadcode_service.errors import AnalysisError

logger = logging.getLogger(__name__)

SANDBOX_MODE = os.environ.get("SANDBOX_MODE", "local")
SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "deadcode-service:dev")

TIMEOUT_SECONDS = 120
MEMORY_LIMIT = "512m"
CPU_LIMIT = "1"
PIDS_LIMIT = "128"  # máximo de procesos (evita "bombas de procesos")
TMP_SIZE = "512m"

OOM_EXIT_CODE = 137  # Linux mata al proceso por exceso de memoria


class AnalysisTimeoutError(AnalysisError):
    def __init__(self) -> None:
        super().__init__(f"El análisis superó el tiempo máximo ({TIMEOUT_SECONDS} s)")


class AnalysisMemoryError(AnalysisError):
    def __init__(self) -> None:
        super().__init__(f"El análisis superó el límite de memoria ({MEMORY_LIMIT})")


class SandboxError(Exception):
    """Fallo inesperado del sandbox (no es culpa del repo)."""


def build_command(container_name: str) -> list[str]:
    """El comando "docker run" con todas las protecciones."""
    return [
        "docker", "run",
        "--rm",                      # destruirlo al terminar
        "-i",                        # recibir datos por stdin
        "--name", container_name,
        "--pull", "never",           # usar solo la imagen local, nunca descargar
        "--network", "none",         # SIN red
        "--read-only",               # disco de solo lectura...
        "--tmpfs", f"/tmp:rw,noexec,nosuid,size={TMP_SIZE}",  # ...salvo /tmp en memoria
        "--user", "65534:65534",     # usuario "nobody"
        "--cap-drop", "ALL",         # sin privilegios especiales de Linux
        "--security-opt", "no-new-privileges",
        "--memory", MEMORY_LIMIT,
        "--memory-swap", MEMORY_LIMIT,  # igual a --memory: sin usar swap
        "--cpus", CPU_LIMIT,
        "--pids-limit", PIDS_LIMIT,
        "--label", "deadcode.sandbox=true",
        SANDBOX_IMAGE,
        "python", "-m", "deadcode_service.sandbox_main",
    ]


def run(tarball: bytes) -> dict:
    """Analiza el .tar.gz y devuelve el resultado (o lanza AnalysisError)."""
    if SANDBOX_MODE == "local":
        output = sandbox_main.process(tarball)
    else:
        output = _run_in_docker(tarball)

    if not output["ok"]:
        raise AnalysisError(output["error"])
    return output["result"]


def _run_in_docker(tarball: bytes) -> dict:
    name = f"deadcode-sandbox-{uuid.uuid4().hex[:12]}"
    try:
        proc = subprocess.run(
            build_command(name), input=tarball, capture_output=True, timeout=TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        # El timeout corta el comando "docker", pero el contenedor podría
        # seguir vivo: lo eliminamos a la fuerza.
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
        raise AnalysisTimeoutError() from None

    if proc.returncode == OOM_EXIT_CODE:
        raise AnalysisMemoryError()
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")[-2000:]
        logger.error("Sandbox %s terminó con código %s: %s", name, proc.returncode, stderr)
        raise SandboxError(f"El sandbox terminó con código {proc.returncode}")

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SandboxError("El sandbox devolvió una respuesta inválida") from exc
