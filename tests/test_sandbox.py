"""Tests del sandbox.

- Comando: que "docker run" lleve TODAS las protecciones.
- Reacciones del worker: reemplazamos subprocess.run por uno falso
  para simular éxito, memoria agotada, tiempo agotado, etc.
- Programa del sandbox: lo ejecutamos de verdad como proceso aparte,
  pasándole un .tar.gz por stdin (igual que hará Docker).
"""

import json
import subprocess
import sys

import pytest

from deadcode_service import sandbox
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
