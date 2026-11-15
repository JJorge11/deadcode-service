"""Ejecuta el análisis en un contenedor desechable (sandbox).

El worker NO analiza el código ajeno en su propio contenedor (que tiene
la contraseña de Postgres y acceso a Redis). En su lugar crea, para
cada análisis, un contenedor nuevo:

  - sin red: no ve Postgres, Redis ni internet
  - sin variables de entorno: no recibe ninguna contraseña
  - disco de solo lectura; solo /tmp para escribir
  - usuario "nobody", sin privilegios de Linux (cap-drop ALL)
  - límites de memoria, CPU y tiempo
  - se destruye entero al terminar

El worker le pasa el .tar.gz por stdin y recibe el JSON por stdout.

Modos (variable SANDBOX_MODE):
  - "docker":     "docker run" (Docker Compose).
  - "kubernetes": un POD desechable en el namespace de sandboxes, con
                  "kubectl run -i" (clúster). Las reglas de ese
                  namespace (Pod Security "restricted", NetworkPolicy
                  sin red, cuota) las impone Kubernetes: ver
                  charts/deadcode-service/templates/sandbox.yaml.
  - "local":      analiza en el mismo proceso, SIN aislamiento. Solo
                  para tests y desarrollo en Windows.
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
SANDBOX_NAMESPACE = os.environ.get("SANDBOX_NAMESPACE", "deadcode-sandboxes")

TIMEOUT_SECONDS = 120
MEMORY_LIMIT = "512m"
CPU_LIMIT = "1"
PIDS_LIMIT = "128"  # máximo de procesos (evita "bombas de procesos")
TMP_SIZE = "512m"

OOM_EXIT_CODE = 137  # Linux mata al proceso por exceso de memoria

# Kubernetes escribe las unidades distinto: "512m" sería 0,512 BYTES.
K8S_MEMORY_LIMIT = "512Mi"
K8S_TMP_SIZE = "512Mi"
# Tiempo extra para crear y arrancar el pod antes de que corra el reloj.
K8S_STARTUP_SECONDS = 30
# kubectl guarda una caché en la carpeta personal; en el worker el disco
# es de solo lectura, así que la ponemos en /tmp.
KUBECTL_CACHE_DIR = "/tmp/.kube-cache"


class AnalysisTimeoutError(AnalysisError):
    def __init__(self) -> None:
        super().__init__(f"El análisis superó el tiempo máximo ({TIMEOUT_SECONDS} s)")


class AnalysisMemoryError(AnalysisError):
    def __init__(self) -> None:
        super().__init__(f"El análisis superó el límite de memoria ({MEMORY_LIMIT})")


class SandboxError(Exception):
    """Fallo inesperado del sandbox (no es culpa del repo)."""


def run(tarball: bytes) -> dict:
    """Analiza el .tar.gz y devuelve el resultado (o lanza AnalysisError)."""
    if SANDBOX_MODE == "local":
        output = sandbox_main.process(tarball)
    elif SANDBOX_MODE == "docker":
        output = _run_in_docker(tarball)
    elif SANDBOX_MODE == "kubernetes":
        output = _run_in_kubernetes(tarball)
    else:
        # Un error de tipeo ("kubernets") NO debe caer en otro modo.
        raise SandboxError(f"SANDBOX_MODE desconocido: {SANDBOX_MODE!r}")

    if not output["ok"]:
        raise AnalysisError(output["error"])
    return output["result"]


def _parse_output(stdout: bytes) -> dict:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise SandboxError("El sandbox devolvió una respuesta inválida") from exc


def _log_failure(name: str, proc: subprocess.CompletedProcess) -> None:
    stderr = proc.stderr.decode("utf-8", errors="replace")[-2000:]
    logger.error("Sandbox %s terminó con código %s: %s", name, proc.returncode, stderr)


# --- Modo docker -------------------------------------------------------


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
        _log_failure(name, proc)
        raise SandboxError(f"El sandbox terminó con código {proc.returncode}")
    return _parse_output(proc.stdout)


# --- Modo kubernetes ---------------------------------------------------


def _kubectl(*args: str) -> list[str]:
    """Un comando kubectl siempre en el namespace de sandboxes."""
    return ["kubectl", *args, "--namespace", SANDBOX_NAMESPACE, "--cache-dir", KUBECTL_CACHE_DIR]


def build_pod_spec() -> dict:
    """El pod desechable: mismas protecciones que "docker run".

    El namespace además RECHAZA cualquier pod que no las cumpla (Pod
    Security "restricted"), y su NetworkPolicy le quita la red.
    """
    return {
        "apiVersion": "v1",
        "metadata": {"labels": {"deadcode.sandbox": "true"}},
        "spec": {
            "serviceAccountName": "sandbox",
            "automountServiceAccountToken": False,  # sin credenciales del clúster
            "enableServiceLinks": False,  # sin variables con direcciones de servicios
            "restartPolicy": "Never",  # si falla, NO reintentar código ajeno
            "activeDeadlineSeconds": TIMEOUT_SECONDS,  # Kubernetes lo mata al vencer
            "terminationGracePeriodSeconds": 0,  # matar de inmediato (sin esperar 30 s)
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 65534,  # usuario "nobody"
                "runAsGroup": 65534,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "sandbox",
                    "image": SANDBOX_IMAGE,
                    # El nodo (no el pod) descarga la imagen si falta: la
                    # NetworkPolicy no lo impide. La etiqueta sha-<commit>
                    # es inmutable, así que no hay sorpresas.
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["python", "-m", "deadcode_service.sandbox_main"],
                    "stdin": True,  # recibe el .tar.gz por stdin...
                    "stdinOnce": True,  # ...una sola vez
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "128Mi"},
                        "limits": {"cpu": CPU_LIMIT, "memory": K8S_MEMORY_LIMIT},
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "readOnlyRootFilesystem": True,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
                }
            ],
            # /tmp en memoria (cuenta dentro del límite de memoria).
            "volumes": [{"name": "tmp", "emptyDir": {"medium": "Memory", "sizeLimit": K8S_TMP_SIZE}}],
        },
    }


def build_kubectl_command(pod_name: str) -> list[str]:
    """"kubectl run -i": crea el pod, le conecta stdin/stdout y espera."""
    return _kubectl(
        "run", pod_name,
        "--image", SANDBOX_IMAGE,  # obligatorio, pero manda el de --overrides
        "--restart", "Never",
        "-i",                      # pasarle nuestro stdin (el .tar.gz)
        "-q",                      # sin mensajes extra: stdout = solo el JSON
        "--overrides", json.dumps(build_pod_spec()),
    )


def _pod_failure_reason(pod_name: str) -> str | None:
    """Pregunta a Kubernetes por qué murió el pod.

    El código de salida no basta: 137 sale tanto por memoria agotada
    (OOMKilled) como por tiempo agotado (DeadlineExceeded).
    """
    try:
        proc = subprocess.run(
            _kubectl("get", "pod", pod_name, "--output", "json"), capture_output=True, timeout=30
        )
        pod = json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
        return None

    status = pod.get("status", {})
    if status.get("reason") == "DeadlineExceeded":
        return "DeadlineExceeded"
    for container in status.get("containerStatuses", []):
        reason = container.get("state", {}).get("terminated", {}).get("reason")
        if reason:
            return reason
    return None


def _delete_pod(pod_name: str) -> None:
    """Borra el pod. Nunca lanza error: no debe tapar el error original."""
    try:
        subprocess.run(
            _kubectl("delete", "pod", pod_name, "--ignore-not-found", "--wait=false"),
            capture_output=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        logger.warning("No se pudo borrar el pod de sandbox %s", pod_name)


def _run_in_kubernetes(tarball: bytes) -> dict:
    name = f"sandbox-{uuid.uuid4().hex[:12]}"
    try:
        try:
            proc = subprocess.run(
                build_kubectl_command(name),
                input=tarball,
                capture_output=True,
                timeout=TIMEOUT_SECONDS + K8S_STARTUP_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise AnalysisTimeoutError() from None

        if proc.returncode != 0:
            reason = _pod_failure_reason(name)
            if reason == "OOMKilled":
                raise AnalysisMemoryError()
            if reason == "DeadlineExceeded":
                raise AnalysisTimeoutError()
            _log_failure(name, proc)
            raise SandboxError(f"El sandbox terminó con código {proc.returncode} ({reason})")
        return _parse_output(proc.stdout)
    finally:
        # Pase lo que pase, el pod se borra (es desechable).
        _delete_pod(name)
