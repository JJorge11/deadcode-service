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

Nivel 2 (coverage.py): download_dependencies() lanza OTRO pod, el de
DESCARGA, que sí tiene internet (solo PyPI; nunca la red interna) pero
no ejecuta nada del repo: lee su pyproject.toml y baja las wheels. Ver
downloader_main.py y la NetworkPolicy "downloader-internet".

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

import io
import json
import logging
import os
import subprocess
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

from deadcode_service import coverage_main, downloader_main, sandbox_main
from deadcode_service.coverage_main import CoverageError
from deadcode_service.dependencies import DependencyError
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
# Borrar el pod: si la API de Kubernetes falla un momento, reintentar.
# (Pasó de verdad: con la API caída unos segundos, un pod quedó huérfano.)
DELETE_ATTEMPTS = 3
DELETE_RETRY_SECONDS = 2

# Pod de descarga (nivel 2). pip tiene 180 s; el resto es margen para
# arrancar Python y armar el .tar de salida.
DOWNLOAD_TIMEOUT_SECONDS = 240
# Las wheels (máx. 150 MB) se guardan en /tmp, que está EN MEMORIA, y
# además se copian al .tar de salida: por eso más memoria que el análisis.
K8S_DOWNLOAD_MEMORY_LIMIT = "768Mi"
K8S_DOWNLOAD_TMP_SIZE = "256Mi"

# Pod de tests (nivel 2, coverage.py). Plazo total del pod: instalar
# (~20 s), tests (máx. 180 s), informe y margen. Su /tmp va en DISCO: el
# repo + wheels + entorno instalado pesan cientos de MB.
COVERAGE_TIMEOUT_SECONDS = 360
K8S_COVERAGE_MEMORY_LIMIT = "1Gi"
K8S_COVERAGE_TMP_SIZE = "2Gi"


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


def _container_security() -> dict:
    """Seguridad de CADA contenedor del pod (portero y análisis)."""
    return {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }


def build_pod_spec() -> dict:
    """El pod de ANÁLISIS: mismas protecciones que "docker run".

    El namespace además RECHAZA cualquier pod que no las cumpla (Pod
    Security "restricted"), y su NetworkPolicy le quita la red.
    """
    return _pod_spec(
        role="analisis",
        gate=("esperar-sin-red", []),
        container=("sandbox", "deadcode_service.sandbox_main"),
        deadline=TIMEOUT_SECONDS,
        memory=K8S_MEMORY_LIMIT,
        tmp_size=K8S_TMP_SIZE,
    )


def build_download_pod_spec() -> dict:
    """El pod de DESCARGA (nivel 2): mismas protecciones, pero su etiqueta
    deadcode.role=downloader activa la NetworkPolicy "downloader-internet"
    (internet sí, red interna no). Su portero solo exige que la red
    INTERNA esté bloqueada antes de empezar."""
    return _pod_spec(
        role="downloader",
        gate=("esperar-sin-red-interna", ["--solo-red-interna"]),
        container=("descarga", "deadcode_service.downloader_main"),
        deadline=DOWNLOAD_TIMEOUT_SECONDS,
        memory=K8S_DOWNLOAD_MEMORY_LIMIT,
        tmp_size=K8S_DOWNLOAD_TMP_SIZE,
    )


def build_coverage_pod_spec() -> dict:
    """El pod de TESTS (nivel 2): ejecuta código del repo, así que va SIN
    red (mismo portero que el análisis; su etiqueta no abre internet).
    /tmp en disco, no en memoria."""
    return _pod_spec(
        role="tests",
        gate=("esperar-sin-red", []),
        container=("tests", "deadcode_service.coverage_main"),
        deadline=COVERAGE_TIMEOUT_SECONDS,
        memory=K8S_COVERAGE_MEMORY_LIMIT,
        tmp_size=K8S_COVERAGE_TMP_SIZE,
        tmp_in_memory=False,
    )


def _pod_spec(
    *, role: str, gate: tuple[str, list[str]], container: tuple[str, str],
    deadline: int, memory: str, tmp_size: str, tmp_in_memory: bool = True,
) -> dict:
    """Lo común de los pods desechables."""
    gate_name, gate_args = gate
    container_name, module = container
    tmp_volume = {"sizeLimit": tmp_size}
    if tmp_in_memory:
        tmp_volume["medium"] = "Memory"
    return {
        "apiVersion": "v1",
        "metadata": {"labels": {"deadcode.sandbox": "true", "deadcode.role": role}},
        "spec": {
            "serviceAccountName": "sandbox",
            "automountServiceAccountToken": False,  # sin credenciales del clúster
            "enableServiceLinks": False,  # sin variables con direcciones de servicios
            "restartPolicy": "Never",  # si falla, NO reintentar código ajeno
            "activeDeadlineSeconds": deadline,  # Kubernetes lo mata al vencer
            "terminationGracePeriodSeconds": 0,  # matar de inmediato (sin esperar 30 s)
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": 65534,  # usuario "nobody"
                "runAsGroup": 65534,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            # PORTERO: corre ANTES del análisis y solo termina bien si
            # confirma que NO hay red. La NetworkPolicy tarda ~1 s en
            # aplicarse a un pod nuevo; así el código ajeno nunca ve ese
            # segundo con red. Si falla, el análisis no arranca.
            "initContainers": [
                {
                    "name": gate_name,
                    "image": SANDBOX_IMAGE,
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["python", "-m", "deadcode_service.wait_no_network", *gate_args],
                    "resources": {
                        "requests": {"cpu": "50m", "memory": "64Mi"},
                        "limits": {"cpu": "500m", "memory": "128Mi"},
                    },
                    "securityContext": _container_security(),
                }
            ],
            "containers": [
                {
                    "name": container_name,
                    "image": SANDBOX_IMAGE,
                    # El nodo (no el pod) descarga la imagen si falta: la
                    # NetworkPolicy no lo impide. La etiqueta sha-<commit>
                    # es inmutable, así que no hay sorpresas.
                    "imagePullPolicy": "IfNotPresent",
                    "command": ["python", "-m", module],
                    "stdin": True,  # recibe el .tar.gz por stdin...
                    "stdinOnce": True,  # ...una sola vez
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "128Mi"},
                        "limits": {"cpu": CPU_LIMIT, "memory": memory},
                    },
                    "securityContext": _container_security(),
                    "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
                }
            ],
            # /tmp en memoria (cuenta dentro del límite de memoria).
            # /tmp: en memoria (cuenta en el límite de memoria) o en disco.
            "volumes": [{"name": "tmp", "emptyDir": tmp_volume}],
        },
    }


def build_kubectl_command(pod_name: str, spec: dict | None = None) -> list[str]:
    """"kubectl run -i": crea el pod, le conecta stdin/stdout y espera."""
    return _kubectl(
        "run", pod_name,
        "--image", SANDBOX_IMAGE,  # obligatorio, pero manda el de --overrides
        "--restart", "Never",
        "-i",                      # pasarle nuestro stdin (el .tar.gz)
        "-q",                      # sin mensajes extra: stdout = solo la respuesta
        "--overrides", json.dumps(spec or build_pod_spec()),
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
    # El portero falló: no se confirmó "sin red" y el análisis no corrió.
    for container in status.get("initContainerStatuses", []):
        if container.get("state", {}).get("terminated", {}).get("exitCode", 0) != 0:
            return "NetworkNotBlocked"
    for container in status.get("containerStatuses", []):
        reason = container.get("state", {}).get("terminated", {}).get("reason")
        if reason:
            return reason
    return None


def _delete_pod(pod_name: str) -> bool:
    """Borra el pod, con reintentos. Devuelve True si lo logró.

    Nunca lanza error: no debe tapar el resultado ni el error original.
    OJO: subprocess.run NO lanza error si kubectl falla (código != 0),
    solo lo devuelve: hay que revisarlo, o el fallo pasa en silencio.
    """
    problem = ""
    for attempt in range(1, DELETE_ATTEMPTS + 1):
        try:
            proc = subprocess.run(
                _kubectl("delete", "pod", pod_name, "--ignore-not-found", "--wait=false"),
                capture_output=True,
                timeout=30,
            )
            if proc.returncode == 0:
                return True
            problem = f"código {proc.returncode}: {proc.stderr.decode(errors='replace').strip()}"
        except (subprocess.TimeoutExpired, OSError) as exc:
            problem = type(exc).__name__
        if attempt < DELETE_ATTEMPTS:
            time.sleep(DELETE_RETRY_SECONDS)

    logger.warning(
        "No se pudo borrar el pod de sandbox %s tras %d intentos (%s)",
        pod_name, DELETE_ATTEMPTS, problem,
    )
    return False


def _run_pod(
    prefix: str, spec: dict, data: bytes, deadline: int,
    timeout_error: Exception, memory_error: Exception,
) -> bytes:
    """Crea un pod desechable, le pasa `data` por stdin y devuelve su
    stdout. Traduce los fallos y SIEMPRE borra el pod al final."""
    name = f"{prefix}-{uuid.uuid4().hex[:12]}"
    try:
        try:
            proc = subprocess.run(
                build_kubectl_command(name, spec),
                input=data,
                capture_output=True,
                timeout=deadline + K8S_STARTUP_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise timeout_error from None

        if proc.returncode != 0:
            reason = _pod_failure_reason(name)
            if reason == "OOMKilled":
                raise memory_error
            if reason == "DeadlineExceeded":
                raise timeout_error
            _log_failure(name, proc)
            raise SandboxError(f"El sandbox terminó con código {proc.returncode} ({reason})")
        return proc.stdout
    finally:
        # Pase lo que pase, el pod se borra (es desechable).
        _delete_pod(name)


def _run_in_kubernetes(tarball: bytes) -> dict:
    stdout = _run_pod(
        "sandbox", build_pod_spec(), tarball, TIMEOUT_SECONDS,
        AnalysisTimeoutError(), AnalysisMemoryError(),
    )
    return _parse_output(stdout)


# --- Nivel 2: descarga de dependencias -----------------------------------


def download_dependencies(tarball: bytes) -> bytes:
    """Descarga las wheels que necesitan los tests del repo.

    Devuelve el .tar de downloader_main (resultado.json + requirements.txt
    + wheels/), listo para pasárselo al pod de tests. Si el repo no se
    puede preparar (sin pyproject.toml, URL directa, paquete inexistente,
    etc.) lanza DependencyError con un motivo claro para el usuario.
    """
    if SANDBOX_MODE == "local":
        output = downloader_main.process(tarball)
    elif SANDBOX_MODE == "kubernetes":
        output = _run_pod(
            "descarga", build_download_pod_spec(), tarball, DOWNLOAD_TIMEOUT_SECONDS,
            DependencyError(
                f"La descarga de dependencias superó el tiempo máximo ({DOWNLOAD_TIMEOUT_SECONDS} s)"
            ),
            DependencyError(
                f"La descarga de dependencias superó el límite de memoria ({K8S_DOWNLOAD_MEMORY_LIMIT})"
            ),
        )
    elif SANDBOX_MODE == "docker":
        # En Compose no hay cómo dejarle internet SIN la red de tu PC (donde
        # están Postgres y Redis): antes que inseguro, no disponible.
        raise DependencyError("El análisis con tests (nivel 2) solo está disponible en Kubernetes")
    else:
        raise SandboxError(f"SANDBOX_MODE desconocido: {SANDBOX_MODE!r}")

    result = read_download_result(output)
    if not result.get("ok"):
        raise DependencyError(result.get("error") or "No se pudieron preparar las dependencias")
    return output


def run_coverage(tarball: bytes, deps: bytes) -> dict:
    """Corre los tests del repo con coverage, SIN red (pod de tests).

    deps = la salida de download_dependencies. Devuelve {"tests": ...,
    "coverage": ...} (ver coverage_main) o lanza CoverageError con un
    motivo claro para el usuario.
    """
    data = coverage_main.pack_input(tarball, deps)
    if SANDBOX_MODE == "local":
        with tempfile.TemporaryDirectory(prefix="deadcode-cov-") as tmp:
            output = coverage_main.process(data, Path(tmp))
    elif SANDBOX_MODE == "kubernetes":
        output = _parse_output(_run_pod(
            "tests", build_coverage_pod_spec(), data, COVERAGE_TIMEOUT_SECONDS,
            CoverageError(f"Los tests del repo superaron el tiempo máximo ({COVERAGE_TIMEOUT_SECONDS} s)"),
            CoverageError(f"Los tests del repo superaron el límite de memoria ({K8S_COVERAGE_MEMORY_LIMIT})"),
        ))
    elif SANDBOX_MODE == "docker":
        raise CoverageError("El análisis con tests (nivel 2) solo está disponible en Kubernetes")
    else:
        raise SandboxError(f"SANDBOX_MODE desconocido: {SANDBOX_MODE!r}")

    if not output.get("ok"):
        raise CoverageError(output.get("error") or "No se pudieron ejecutar los tests")
    return output["result"]


def read_download_result(output: bytes) -> dict:
    """Lee resultado.json del .tar que devolvió el pod de descarga."""
    try:
        with tarfile.open(fileobj=io.BytesIO(output), mode="r:") as tar:
            member = tar.extractfile("resultado.json")
            result = json.load(member)
    except (tarfile.TarError, KeyError, AttributeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        # KeyError: no está; AttributeError: extractfile devolvió None (no es archivo).
        raise SandboxError("El pod de descarga devolvió una respuesta inválida") from exc
    if not isinstance(result, dict):
        raise SandboxError("El pod de descarga devolvió una respuesta inválida")
    return result
