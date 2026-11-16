"""Tests del sandbox.

- Comando: que "docker run" y el pod de Kubernetes lleven TODAS las
  protecciones.
- Reacciones del worker: reemplazamos subprocess.run por uno falso
  para simular éxito, memoria agotada, tiempo agotado, etc.
- Programa del sandbox: lo ejecutamos de verdad como proceso aparte,
  pasándole un .tar.gz por stdin (igual que hará Docker).
"""

import json
import subprocess
import sys

import pytest

from deadcode_service import coverage_main, downloader_main, sandbox
from deadcode_service.coverage_main import CoverageError
from deadcode_service.dependencies import DependencyError
from deadcode_service.errors import AnalysisError
from tests.helpers import make_tarball

TARBALL = make_tarball({"app.py": "def olvidada():\n    pass\n"}, commit="c0ffee")


# --- El comando lleva todas las protecciones --------------------------


def _flag_value(command, flag):
    return command[command.index(flag) + 1]


def test_comando_docker_tiene_todas_las_protecciones():
    command = sandbox.build_command("deadcode-sandbox-test")

    assert command[:2] == ["docker", "run"]
    assert "--rm" in command and "-i" in command and "--read-only" in command
    assert _flag_value(command, "--network") == "none"
    assert _flag_value(command, "--user") == "65534:65534"
    assert _flag_value(command, "--cap-drop") == "ALL"
    assert _flag_value(command, "--security-opt") == "no-new-privileges"
    assert _flag_value(command, "--pull") == "never"
    assert _flag_value(command, "--memory") == _flag_value(command, "--memory-swap")
    assert _flag_value(command, "--pids-limit") == sandbox.PIDS_LIMIT
    assert "noexec" in _flag_value(command, "--tmpfs")
    # Ninguna variable de entorno ni carpeta compartida: nada de secretos.
    assert "-e" not in command and "--env" not in command and "--env-file" not in command
    assert "-v" not in command and "--volume" not in command and "--mount" not in command


# --- Modo local (tests y Windows) --------------------------------------


def test_modo_local_analiza_y_devuelve_el_commit():
    result = sandbox.run(TARBALL)

    assert result["commit"] == "c0ffee"
    assert [f["name"] for f in result["dead_code"]] == ["olvidada"]


def test_modo_local_error_esperado():
    with pytest.raises(AnalysisError, match="no contiene archivos Python"):
        sandbox.run(make_tarball({"README.md": "hola"}))


# --- Modo docker: reacciones del worker -------------------------------


@pytest.fixture
def fake_docker(monkeypatch):
    """Reemplaza subprocess.run. Guarda las llamadas para revisarlas."""
    monkeypatch.setattr(sandbox, "SANDBOX_MODE", "docker")
    calls = []

    def install(returncode=0, stdout=b"", timeout=False):
        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if timeout and command[:2] == ["docker", "run"]:
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
            return subprocess.CompletedProcess(command, returncode, stdout, b"detalle en stderr")

        monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
        return calls

    return install


def test_docker_exito_pasa_el_tarball_por_stdin(fake_docker):
    calls = fake_docker(stdout=json.dumps({"ok": True, "result": {"commit": "abc"}}).encode())

    result = sandbox.run(TARBALL)

    assert result == {"commit": "abc"}
    [(command, kwargs)] = calls
    assert kwargs["input"] == TARBALL  # el repo entra por stdin
    assert kwargs["timeout"] == sandbox.TIMEOUT_SECONDS


def test_docker_error_esperado_del_repo(fake_docker):
    fake_docker(stdout=json.dumps({"ok": False, "error": "El repositorio no contiene archivos Python"}).encode())

    with pytest.raises(AnalysisError, match="no contiene archivos Python"):
        sandbox.run(TARBALL)


def test_docker_memoria_agotada(fake_docker):
    fake_docker(returncode=137)

    with pytest.raises(sandbox.AnalysisMemoryError, match="límite de memoria"):
        sandbox.run(TARBALL)


def test_docker_tiempo_agotado_elimina_el_contenedor(fake_docker):
    calls = fake_docker(timeout=True)

    with pytest.raises(sandbox.AnalysisTimeoutError, match="tiempo máximo"):
        sandbox.run(TARBALL)

    run_command = calls[0][0]
    name = run_command[run_command.index("--name") + 1]
    assert calls[1][0] == ["docker", "rm", "-f", name]  # se elimina a la fuerza


@pytest.mark.parametrize("returncode, stdout", [(1, b""), (0, b"esto no es json")])
def test_docker_fallo_inesperado_no_es_culpa_del_repo(fake_docker, returncode, stdout):
    fake_docker(returncode=returncode, stdout=stdout)

    with pytest.raises(sandbox.SandboxError):
        sandbox.run(TARBALL)


def test_modo_desconocido_es_un_error(monkeypatch):
    """Un error de tipeo NO debe caer en otro modo (ni en "local")."""
    monkeypatch.setattr(sandbox, "SANDBOX_MODE", "kubernets")

    with pytest.raises(sandbox.SandboxError, match="desconocido"):
        sandbox.run(TARBALL)


# --- Modo kubernetes: el pod lleva todas las protecciones -------------


POD_SPECS = [
    # (cómo armarlo, /tmp, plazo, memoria)
    pytest.param(
        sandbox.build_pod_spec, {"medium": "Memory", "sizeLimit": sandbox.K8S_TMP_SIZE},
        sandbox.TIMEOUT_SECONDS, "512Mi", id="analisis",
    ),
    pytest.param(
        sandbox.build_download_pod_spec, {"medium": "Memory", "sizeLimit": sandbox.K8S_DOWNLOAD_TMP_SIZE},
        sandbox.DOWNLOAD_TIMEOUT_SECONDS, "768Mi", id="descarga",
    ),
    pytest.param(
        # /tmp en DISCO (sin "medium"): repo + wheels + entorno instalado.
        sandbox.build_coverage_pod_spec, {"sizeLimit": sandbox.K8S_COVERAGE_TMP_SIZE},
        sandbox.COVERAGE_TIMEOUT_SECONDS, "1Gi", id="tests",
    ),
]


@pytest.mark.parametrize("build, tmp_volume, deadline, memory", POD_SPECS)
def test_pod_cumple_pod_security_restricted(build, tmp_volume, deadline, memory):
    spec = build()["spec"]
    [container] = spec["containers"]

    # Sin credenciales del clúster ni variables de entorno.
    assert spec["automountServiceAccountToken"] is False
    assert spec["enableServiceLinks"] is False
    assert "env" not in container and "envFrom" not in container
    # Usuario sin privilegios.
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["securityContext"]["runAsUser"] == 65534
    assert spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    # Solo /tmp, en memoria; nada del nodo (hostPath).
    assert spec["volumes"] == [{"name": "tmp", "emptyDir": tmp_volume}]
    # Desechable, con límites y reloj.
    assert spec["restartPolicy"] == "Never"
    assert spec["activeDeadlineSeconds"] == deadline
    assert container["resources"]["limits"]["memory"] == memory  # ¡"Mi", no "m"!
    assert container["stdin"] is True and container["stdinOnce"] is True
    # Portero con las mismas protecciones y con límites (lo exige la cuota).
    [portero] = spec["initContainers"]
    assert portero["securityContext"] == container["securityContext"]
    assert "memory" in portero["resources"]["limits"] and "cpu" in portero["resources"]["limits"]
    assert "env" not in portero and "volumeMounts" not in portero


def test_solo_el_pod_de_descarga_lleva_la_etiqueta_que_abre_internet():
    """La NetworkPolicy "downloader-internet" se activa por esta etiqueta:
    si el pod de ANÁLISIS (código ajeno) la llevara, tendría internet."""
    analisis = sandbox.build_pod_spec()
    descarga = sandbox.build_download_pod_spec()

    tests = sandbox.build_coverage_pod_spec()
    assert analisis["metadata"]["labels"]["deadcode.role"] == "analisis"
    assert tests["metadata"]["labels"]["deadcode.role"] == "tests"  # ejecuta código ajeno: SIN red
    assert descarga["metadata"]["labels"]["deadcode.role"] == "downloader"
    # Los dos cuentan como sandbox (alerta de huérfanos, panel de Grafana).
    assert analisis["metadata"]["labels"]["deadcode.sandbox"] == "true"
    assert descarga["metadata"]["labels"]["deadcode.sandbox"] == "true"


def test_pod_de_descarga_portero_y_programa():
    spec = sandbox.build_download_pod_spec()["spec"]
    [portero] = spec["initContainers"]
    [container] = spec["containers"]

    assert portero["command"] == [
        "python", "-m", "deadcode_service.wait_no_network", "--solo-red-interna",
    ]
    assert container["command"] == ["python", "-m", "deadcode_service.downloader_main"]


def test_pod_tiene_portero_que_confirma_sin_red():
    spec = sandbox.build_pod_spec()["spec"]
    [portero] = spec["initContainers"]
    [container] = spec["containers"]

    assert portero["command"] == ["python", "-m", "deadcode_service.wait_no_network"]
    assert portero["image"] == container["image"]
    # Mismas protecciones que el análisis, y con límites (lo exige la cuota).
    assert portero["securityContext"] == container["securityContext"]
    assert "memory" in portero["resources"]["limits"] and "cpu" in portero["resources"]["limits"]
    assert "env" not in portero and "volumeMounts" not in portero


def test_kubernetes_portero_fallido_no_es_culpa_del_repo(fake_kubectl):
    status = {"initContainerStatuses": [{"state": {"terminated": {"exitCode": 1, "reason": "Error"}}}]}
    calls = fake_kubectl(returncode=1, pod_status=status)

    with pytest.raises(sandbox.SandboxError, match="NetworkNotBlocked"):
        sandbox.run(TARBALL)

    _assert_pod_deleted(calls)


def test_comando_kubectl_run():
    command = sandbox.build_kubectl_command("sandbox-test")

    assert command[:3] == ["kubectl", "run", "sandbox-test"]
    assert "-i" in command and "-q" in command  # stdin, y stdout solo con el JSON
    assert _flag_value(command, "--namespace") == sandbox.SANDBOX_NAMESPACE
    assert _flag_value(command, "--restart") == "Never"
    assert json.loads(_flag_value(command, "--overrides")) == sandbox.build_pod_spec()


# --- Modo kubernetes: reacciones del worker ---------------------------


@pytest.fixture
def fake_kubectl(monkeypatch):
    """Reemplaza subprocess.run y responde según el subcomando de kubectl:
    "run" (el análisis), "get" (por qué murió el pod) y "delete"."""
    monkeypatch.setattr(sandbox, "SANDBOX_MODE", "kubernetes")
    calls = []

    monkeypatch.setattr(sandbox.time, "sleep", lambda seconds: None)  # sin esperas reales

    def install(returncode=0, stdout=b"", timeout=False, pod_status=None, delete_fails=False, delete_results=()):
        """delete_results: respuestas de cada intento de borrado, en orden:
        un código de salida (int) o "timeout". Después del último: 0."""
        pending_deletes = list(delete_results)

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            action = command[1]
            if action == "run":
                if timeout:
                    raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
                return subprocess.CompletedProcess(command, returncode, stdout, b"detalle")
            if action == "get":
                return subprocess.CompletedProcess(command, 0, json.dumps({"status": pod_status or {}}).encode(), b"")
            if action == "delete":
                if delete_fails:
                    raise subprocess.TimeoutExpired(command, 30)
                result = pending_deletes.pop(0) if pending_deletes else 0
                if result == "timeout":
                    raise subprocess.TimeoutExpired(command, 30)
                stderr = b"" if result == 0 else b"Unable to connect to the server: EOF"
                return subprocess.CompletedProcess(command, result, b"", stderr)
            raise AssertionError(f"comando inesperado: {command}")

        monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
        return calls

    return install


def _actions(calls):
    return [command[1] for command, _ in calls]


def _pod_name(calls):
    return calls[0][0][2]  # kubectl run <nombre>


def _assert_pod_deleted(calls):
    delete_command = calls[-1][0]
    assert delete_command[1:4] == ["delete", "pod", _pod_name(calls)]


def _oom_status():
    return {"containerStatuses": [{"state": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}]}


def test_kubernetes_exito_pasa_el_tarball_y_borra_el_pod(fake_kubectl):
    calls = fake_kubectl(stdout=json.dumps({"ok": True, "result": {"commit": "abc"}}).encode())

    result = sandbox.run(TARBALL)

    assert result == {"commit": "abc"}
    assert calls[0][1]["input"] == TARBALL  # el repo entra por stdin
    assert _actions(calls) == ["run", "delete"]
    _assert_pod_deleted(calls)


def test_kubernetes_memoria_agotada(fake_kubectl):
    calls = fake_kubectl(returncode=137, pod_status=_oom_status())

    with pytest.raises(sandbox.AnalysisMemoryError, match="límite de memoria"):
        sandbox.run(TARBALL)

    assert _actions(calls) == ["run", "get", "delete"]
    _assert_pod_deleted(calls)


def test_kubernetes_plazo_vencido_es_tiempo_no_memoria(fake_kubectl):
    """Kubernetes también devuelve 137 al vencer el plazo: hay que
    preguntarle al pod el motivo, no adivinar por el número."""
    calls = fake_kubectl(returncode=137, pod_status={"phase": "Failed", "reason": "DeadlineExceeded"})

    with pytest.raises(sandbox.AnalysisTimeoutError, match="tiempo máximo"):
        sandbox.run(TARBALL)

    _assert_pod_deleted(calls)


def test_kubernetes_kubectl_no_responde_a_tiempo(fake_kubectl):
    calls = fake_kubectl(timeout=True)

    with pytest.raises(sandbox.AnalysisTimeoutError):
        sandbox.run(TARBALL)

    assert calls[0][1]["timeout"] == sandbox.TIMEOUT_SECONDS + sandbox.K8S_STARTUP_SECONDS
    _assert_pod_deleted(calls)


@pytest.mark.parametrize("returncode, stdout", [(137, b""), (1, b""), (0, b"esto no es json")])
def test_kubernetes_fallo_inesperado_no_es_culpa_del_repo(fake_kubectl, returncode, stdout):
    """Incluye 137 SIN motivo conocido: no se debe culpar a la memoria."""
    calls = fake_kubectl(returncode=returncode, stdout=stdout)

    with pytest.raises(sandbox.SandboxError):
        sandbox.run(TARBALL)

    _assert_pod_deleted(calls)


def test_kubernetes_fallo_al_borrar_no_tapa_el_resultado(fake_kubectl):
    fake_kubectl(stdout=json.dumps({"ok": True, "result": {"commit": "abc"}}).encode(), delete_fails=True)

    assert sandbox.run(TARBALL) == {"commit": "abc"}


_OK = json.dumps({"ok": True, "result": {"commit": "abc"}}).encode()


def test_kubernetes_reintenta_borrar_si_la_api_falla_un_momento(fake_kubectl, caplog):
    """Lo que pasó de verdad: la API de Kubernetes cayó unos segundos,
    "kubectl delete" terminó con código 1 y el pod quedó huérfano."""
    calls = fake_kubectl(stdout=_OK, delete_results=[1, "timeout", 0])

    assert sandbox.run(TARBALL) == {"commit": "abc"}

    assert _actions(calls) == ["run", "delete", "delete", "delete"]
    assert "No se pudo borrar" not in caplog.text  # al final lo logró: sin aviso


def test_kubernetes_si_nunca_puede_borrar_lo_avisa_en_el_log(fake_kubectl, caplog):
    """Antes, un código != 0 de kubectl pasaba en SILENCIO (sin log)."""
    calls = fake_kubectl(stdout=_OK, delete_results=[1] * sandbox.DELETE_ATTEMPTS)

    assert sandbox.run(TARBALL) == {"commit": "abc"}  # el resultado no se pierde

    assert _actions(calls).count("delete") == sandbox.DELETE_ATTEMPTS  # se rinde
    pod = _pod_name(calls)
    assert f"No se pudo borrar el pod de sandbox {pod}" in caplog.text
    assert "Unable to connect to the server" in caplog.text  # con el motivo


def test_fallo_inesperado_no_se_confunde_con_error_del_repo():
    """SandboxError NO es AnalysisError: el usuario verá "Error
    inesperado", no un mensaje que culpe a su repositorio."""
    assert not issubclass(sandbox.SandboxError, AnalysisError)


# --- El programa del sandbox, de verdad --------------------------------


def test_programa_del_sandbox_lee_stdin_y_escribe_json():
    proc = subprocess.run(
        [sys.executable, "-m", "deadcode_service.sandbox_main"],
        input=TARBALL,
        capture_output=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    output = json.loads(proc.stdout)
    assert output["ok"] is True
    assert output["result"]["commit"] == "c0ffee"
    assert [f["name"] for f in output["result"]["dead_code"]] == ["olvidada"]


# --- Nivel 2: pod de descarga de dependencias --------------------------


DOWNLOAD_OK = downloader_main._pack({"ok": True}, ["pytest"])


def test_descarga_kubernetes_ok_devuelve_el_tar_y_borra_el_pod(fake_kubectl):
    calls = fake_kubectl(stdout=DOWNLOAD_OK)

    assert sandbox.download_dependencies(TARBALL) == DOWNLOAD_OK

    run_command, run_kwargs = calls[0]
    assert run_kwargs["input"] == TARBALL
    assert _pod_name(calls).startswith("descarga-")
    assert json.loads(_flag_value(run_command, "--overrides")) == sandbox.build_download_pod_spec()
    _assert_pod_deleted(calls)


def test_descarga_error_del_repo_llega_al_usuario(fake_kubectl):
    stdout = downloader_main._pack({"ok": False, "error": "El repo no tiene pyproject.toml: ..."})
    calls = fake_kubectl(stdout=stdout)

    with pytest.raises(DependencyError, match="no tiene pyproject.toml"):
        sandbox.download_dependencies(TARBALL)

    _assert_pod_deleted(calls)


def test_descarga_memoria_agotada(fake_kubectl):
    calls = fake_kubectl(returncode=137, pod_status=_oom_status())

    with pytest.raises(DependencyError, match="límite de memoria"):
        sandbox.download_dependencies(TARBALL)

    _assert_pod_deleted(calls)


def test_descarga_plazo_vencido(fake_kubectl):
    calls = fake_kubectl(returncode=137, pod_status={"reason": "DeadlineExceeded"})

    with pytest.raises(DependencyError, match="tiempo máximo"):
        sandbox.download_dependencies(TARBALL)

    _assert_pod_deleted(calls)


def test_descarga_portero_fallido_no_es_culpa_del_repo(fake_kubectl):
    status = {"initContainerStatuses": [{"state": {"terminated": {"exitCode": 1}}}]}
    calls = fake_kubectl(returncode=1, pod_status=status)

    with pytest.raises(sandbox.SandboxError, match="NetworkNotBlocked"):
        sandbox.download_dependencies(TARBALL)

    _assert_pod_deleted(calls)


def _tar_with(files):
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@pytest.mark.parametrize(
    "stdout",
    [
        b"",
        b"esto no es un tar",
        _tar_with({"otro.txt": b"x"}),  # falta resultado.json
        _tar_with({"resultado.json": b"no es json"}),
        _tar_with({"resultado.json": b"[1, 2]"}),  # JSON, pero no un objeto
    ],
)
def test_descarga_respuesta_invalida_no_es_culpa_del_repo(fake_kubectl, stdout):
    calls = fake_kubectl(stdout=stdout)

    with pytest.raises(sandbox.SandboxError, match="respuesta inválida"):
        sandbox.download_dependencies(TARBALL)

    _assert_pod_deleted(calls)


def test_descarga_en_docker_no_disponible(monkeypatch):
    """En Compose no se puede dar internet sin la red de tu PC: no se intenta."""
    monkeypatch.setattr(sandbox, "SANDBOX_MODE", "docker")
    monkeypatch.setattr(sandbox.subprocess, "run", lambda *a, **k: pytest.fail("no debe lanzar nada"))

    with pytest.raises(DependencyError, match="solo está disponible en Kubernetes"):
        sandbox.download_dependencies(TARBALL)


def test_descarga_modo_local_usa_el_mismo_programa(monkeypatch):
    """Modo local (tests): mismo downloader_main, sin pod (pip simulado)."""
    monkeypatch.setattr(sandbox, "SANDBOX_MODE", "local")
    monkeypatch.setattr(downloader_main, "_run_pip", lambda req, dest: subprocess.CompletedProcess([], 0, b"", b""))
    tarball = make_tarball({"pyproject.toml": '[project]\nname = "x"\n'})

    output = sandbox.download_dependencies(tarball)

    assert sandbox.read_download_result(output) == {"ok": True}



# --- Nivel 2: pod de tests (coverage) ------------------------------------


COVERAGE_OK = {"ok": True, "result": {"tests": {"exit_code": 0}, "coverage": {"percent": 97.2, "files": {}}}}


def test_pod_de_tests_portero_sin_red_y_programa():
    """Ejecuta código ajeno: el portero COMPLETO (ni internet), no el de descarga."""
    spec = sandbox.build_coverage_pod_spec()["spec"]
    [portero] = spec["initContainers"]
    [container] = spec["containers"]

    assert portero["command"] == ["python", "-m", "deadcode_service.wait_no_network"]
    assert container["command"] == ["python", "-m", "deadcode_service.coverage_main"]


def test_tests_kubernetes_ok_pasa_repo_y_deps_y_borra_el_pod(fake_kubectl):
    calls = fake_kubectl(stdout=json.dumps(COVERAGE_OK).encode())

    assert sandbox.run_coverage(TARBALL, DOWNLOAD_OK) == COVERAGE_OK["result"]

    run_command, run_kwargs = calls[0]
    assert coverage_main.unpack_input(run_kwargs["input"]) == (TARBALL, DOWNLOAD_OK)
    assert _pod_name(calls).startswith("tests-")
    assert json.loads(_flag_value(run_command, "--overrides")) == sandbox.build_coverage_pod_spec()
    _assert_pod_deleted(calls)


def test_tests_error_del_repo_llega_al_usuario(fake_kubectl):
    stdout = json.dumps({"ok": False, "error": "Los tests del repo no pudieron ejecutarse: KeyError: 'X'"})
    calls = fake_kubectl(stdout=stdout.encode())

    with pytest.raises(CoverageError, match="no pudieron ejecutarse"):
        sandbox.run_coverage(TARBALL, DOWNLOAD_OK)

    _assert_pod_deleted(calls)


@pytest.mark.parametrize(
    "pod_status, message",
    [(_oom_status(), "límite de memoria"), ({"reason": "DeadlineExceeded"}, "tiempo máximo")],
)
def test_tests_memoria_o_plazo(fake_kubectl, pod_status, message):
    calls = fake_kubectl(returncode=137, pod_status=pod_status)

    with pytest.raises(CoverageError, match=message):
        sandbox.run_coverage(TARBALL, DOWNLOAD_OK)

    _assert_pod_deleted(calls)


def test_tests_respuesta_invalida_no_es_culpa_del_repo(fake_kubectl):
    fake_kubectl(stdout=b"esto no es json")

    with pytest.raises(sandbox.SandboxError):
        sandbox.run_coverage(TARBALL, DOWNLOAD_OK)


def test_tests_en_docker_no_disponible(monkeypatch):
    monkeypatch.setattr(sandbox, "SANDBOX_MODE", "docker")
    monkeypatch.setattr(sandbox.subprocess, "run", lambda *a, **k: pytest.fail("no debe lanzar nada"))

    with pytest.raises(CoverageError, match="solo está disponible en Kubernetes"):
        sandbox.run_coverage(TARBALL, DOWNLOAD_OK)


def test_tests_modo_local_usa_el_mismo_programa(monkeypatch):
    monkeypatch.setattr(sandbox, "SANDBOX_MODE", "local")
    seen = {}

    def fake_process(data, workdir):
        seen["input"] = coverage_main.unpack_input(data)
        return COVERAGE_OK

    monkeypatch.setattr(coverage_main, "process", fake_process)

    assert sandbox.run_coverage(TARBALL, DOWNLOAD_OK) == COVERAGE_OK["result"]
    assert seen["input"] == (TARBALL, DOWNLOAD_OK)
