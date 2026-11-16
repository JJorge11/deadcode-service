"""Tests de coverage_main.py (el programa del pod de tests, nivel 2).

Los pasos que ejecutan procesos (venv, pip, pytest, coverage) se simulan
reemplazando run_limited: en Windows no se puede, y sin internet no hay
wheels. Las pruebas reales de run_limited (plazo con Ctrl+C, salida a un
log) corren solo en Linux (CI). La prueba completa se hizo en Docker sin
red (ver CLAUDE.md).
"""

import io
import json
import subprocess
import sys
import tarfile

import pytest

from deadcode_service import coverage_main as cm
from deadcode_service import downloader_main
from deadcode_service.fetcher import InvalidArchiveError
from tests.helpers import make_tarball

REPO = make_tarball({
    "pyproject.toml": '[project]\nname = "x"\n',
    "src/x/__init__.py": "def f():\n    return 1\n",
    "tests/test_x.py": "def test_f(): pass\n",
    "tests/data.json": "{}",  # no es .py, pero los tests lo necesitan
})
DEPS = downloader_main._pack({"ok": True}, ["pytest", "coverage==7.17.3"])


def _tar(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


# --- Entrada -----------------------------------------------------------


def test_pack_y_unpack_ida_y_vuelta():
    assert cm.unpack_input(cm.pack_input(REPO, DEPS)) == (REPO, DEPS)


@pytest.mark.parametrize("data", [b"", b"basura", _tar({"repo.tar.gz": b"x"})])  # falta deps.tar
def test_entrada_invalida(data):
    with pytest.raises(InvalidArchiveError):
        cm.unpack_input(data)


def test_extract_deps_solo_requirements_y_wheels(tmp_path):
    deps = _tar({
        "requirements.txt": b"pytest\n",
        "wheels/pytest-9.0-py3-none-any.whl": b"w",
        "wheels/sub/otra.whl": b"w",  # subcarpeta: no
        "wheels/script.sh": b"rm -rf /",  # no es wheel: no
        "otro.txt": b"x",
    })
    wheels, requirements = cm.extract_deps(deps, tmp_path / "deps")

    assert requirements.read_text() == "pytest\n"
    assert [p.name for p in wheels.iterdir()] == ["pytest-9.0-py3-none-any.whl"]
    assert not (tmp_path / "deps" / "otro.txt").exists()


def test_extract_deps_sin_requirements(tmp_path):
    with pytest.raises(cm.CoverageError, match="requirements.txt"):
        cm.extract_deps(_tar({"wheels/a.whl": b"w"}), tmp_path / "deps")


def test_entorno_minimo_no_hereda_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "secreto")
    env = cm.clean_env(tmp_path, tmp_path / "venv")

    assert "POSTGRES_PASSWORD" not in env
    assert env["PATH"].startswith(str(tmp_path / "venv" / "bin"))
    assert env["HOME"].startswith(str(tmp_path))


# --- Resumen para el usuario -------------------------------------------


@pytest.mark.parametrize(
    "log, expected",
    [
        ("....\n===== 297 passed in 2.02s =====\n", "297 passed in 2.02s"),
        ("x\n= 1 failed, 2 passed in 7.22s =\nKeyboardInterrupt\n", "1 failed, 2 passed in 7.22s"),
        ("ImportError while loading conftest\nE   KeyError: 'POSTGRES_USER'\nwarn: no data\n", "KeyError: 'POSTGRES_USER'"),
        ("algo raro\nultima linea\n", "ultima linea"),
        ("", ""),
    ],
)
def test_resumen_de_los_tests(tmp_path, log, expected):
    path = tmp_path / "tests.log"
    path.write_text(log, encoding="utf-8")
    assert cm.summarize_tests(path) == expected


def test_resumen_se_corta(tmp_path):
    path = tmp_path / "tests.log"
    path.write_text("E   " + "x" * 1000, encoding="utf-8")
    assert len(cm.summarize_tests(path)) == cm.MAX_SUMMARY_CHARS


# --- Flujo completo con procesos simulados ------------------------------


@pytest.fixture
def fake_run(monkeypatch):
    """Reemplaza run_limited. Cada paso se reconoce por su comando; se
    puede configurar qué devuelve cada uno."""
    calls = []
    config = {
        "venv": (0, False), "deps": (0, False), "editable": (0, False),
        "pytest": (0, False), "report": None, "log_text": {},
    }

    def step(command):
        if command[1:3] == ["-m", "venv"]:
            return "venv"
        if "--editable" in command:
            return "editable"
        if "--requirement" in command:
            return "deps"
        if "run" in command:
            return "pytest"
        return "json"

    def fake(command, *, cwd, env, log, timeout):
        name = step(command)
        calls.append((name, command, cwd, env))
        if name in config["log_text"]:
            with open(log, "a", encoding="utf-8") as out:
                out.write(config["log_text"][name])
        if name == "json":
            if config["report"] is None:
                return 1, False
            out = command[command.index("-o") + 1]
            with open(out, "w", encoding="utf-8") as f:
                json.dump(config["report"](cwd), f)
            return 0, False
        return config[name]

    monkeypatch.setattr(cm, "run_limited", fake)
    return calls, config


def _report(repo):
    """Como coverage json: rutas absolutas (instalación editable)."""
    return {
        "files": {
            str(repo / "src" / "x" / "__init__.py"): {"executed_lines": [2, 1], "missing_lines": []},
            "/otro/lugar/fuera.py": {"executed_lines": [1], "missing_lines": []},
            "../../escapa.py": {"executed_lines": [1], "missing_lines": []},
        },
        "totals": {"percent_covered": 66.666},
    }


def test_flujo_completo_ok(tmp_path, fake_run):
    calls, config = fake_run
    config["report"] = _report
    config["log_text"] = {"pytest": "= 1 passed in 0.1s =\n"}

    output = cm.process(cm.pack_input(REPO, DEPS), tmp_path)

    assert output == {"ok": True, "result": {
        "tests": {"exit_code": 0, "timed_out": False, "summary": "1 passed in 0.1s",
                  "project_installed": True, "source": "src"},
        # Rutas relativas al repo; lo de fuera del repo se descarta.
        "coverage": {"percent": 66.7, "files": {"src/x/__init__.py": {"executed": [1, 2], "missing": []}}},
    }}
    assert [c[0] for c in calls] == ["venv", "deps", "editable", "pytest", "json"]
    # El repo trae TODOS sus archivos (no solo .py).
    repo = calls[3][2]
    assert (repo / "tests" / "data.json").is_file()


def test_pip_sin_red_y_nuestra_configuracion_de_coverage(tmp_path, fake_run):
    calls, config = fake_run
    config["report"] = _report
    cm.process(cm.pack_input(REPO, DEPS), tmp_path)

    by_step = {name: command for name, command, _, _ in calls}
    for name in ("deps", "editable"):
        assert "--no-index" in by_step[name]  # nunca buscar en internet
    pytest_cmd = by_step["pytest"]
    assert "--continue-on-collection-errors" in pytest_cmd
    rc = next(a for a in pytest_cmd if a.startswith("--rcfile="))
    rc_text = open(rc.removeprefix("--rcfile="), encoding="utf-8").read()
    assert "source = src" in rc_text and "parallel = false" in rc_text


def test_falla_instalar_dependencias_muestra_el_motivo(tmp_path, fake_run):
    _, config = fake_run
    config["deps"] = (1, False)
    config["log_text"] = {"deps": "ERROR: Could not find a version that satisfies the requirement foo\n"}

    output = cm.process(cm.pack_input(REPO, DEPS), tmp_path)

    assert output == {"ok": False, "error": "No se pudieron instalar las dependencias: "
                      "ERROR: Could not find a version that satisfies the requirement foo"}


def test_instalar_dependencias_tarda_demasiado(tmp_path, fake_run):
    _, config = fake_run
    config["deps"] = (-9, True)

    output = cm.process(cm.pack_input(REPO, DEPS), tmp_path)

    assert "tiempo máximo" in output["error"]


def test_si_el_repo_no_se_instala_usa_pythonpath(tmp_path, fake_run):
    calls, config = fake_run
    config["editable"] = (1, False)
    config["report"] = _report

    output = cm.process(cm.pack_input(REPO, DEPS), tmp_path)

    assert output["result"]["tests"]["project_installed"] is False
    pytest_env = next(env for name, _, _, env in calls if name == "pytest")
    assert pytest_env["PYTHONPATH"].endswith("src")


def test_algunos_tests_fallan_igual_sirve(tmp_path, fake_run):
    _, config = fake_run
    config["pytest"] = (1, False)
    config["report"] = _report

    assert cm.process(cm.pack_input(REPO, DEPS), tmp_path)["ok"] is True


def test_tests_que_no_arrancan_son_un_error_claro(tmp_path, fake_run):
    """Como este mismo repo: pide Postgres y pytest ni empieza. Con 0%
    no se debe decir "nada se usa": se dice por qué no corrieron."""
    _, config = fake_run
    config["pytest"] = (4, False)
    config["log_text"] = {"pytest": "E   KeyError: 'POSTGRES_USER'\n"}
    config["report"] = lambda repo: {"files": {}, "totals": {"percent_covered": 0}}

    output = cm.process(cm.pack_input(REPO, DEPS), tmp_path)

    assert output == {"ok": False, "error": "Los tests del repo no pudieron ejecutarse: KeyError: 'POSTGRES_USER'"}


def test_plazo_vencido_entrega_lo_medido(tmp_path, fake_run):
    _, config = fake_run
    config["pytest"] = (2, True)  # Ctrl+C: pytest sale con 2
    config["report"] = _report

    output = cm.process(cm.pack_input(REPO, DEPS), tmp_path)

    assert output["ok"] is True
    assert output["result"]["tests"]["timed_out"] is True


def test_sin_informe_de_coverage(tmp_path, fake_run):
    _, config = fake_run
    config["report"] = None

    output = cm.process(cm.pack_input(REPO, DEPS), tmp_path)

    assert "sin datos de cobertura" in output["error"]


def test_informe_roto(tmp_path, fake_run):
    _, config = fake_run
    config["report"] = lambda repo: {"files": "no es un dict"}

    assert "no es válido" in cm.process(cm.pack_input(REPO, DEPS), tmp_path)["error"]


# --- run_limited de verdad (solo Linux: grupos de procesos y señales) ---

linux_only = pytest.mark.skipif(sys.platform == "win32", reason="señales POSIX (corre en el CI)")


@linux_only
def test_run_limited_la_salida_va_al_log_no_a_nuestro_stdout(tmp_path, capfd):
    log = tmp_path / "out.log"
    code, timed_out = cm.run_limited(
        [sys.executable, "-c", "print('hablo por ti')"], cwd=tmp_path, env={}, log=log, timeout=10
    )

    assert (code, timed_out) == (0, False)
    assert "hablo por ti" in log.read_text()
    assert "hablo por ti" not in capfd.readouterr().out


@linux_only
def test_run_limited_al_vencer_manda_ctrl_c_y_deja_terminar(tmp_path):
    """Como pytest + coverage: atrapa Ctrl+C, guarda y sale ordenado."""
    script = (
        "import time\n"
        "try:\n    time.sleep(60)\n"
        "except KeyboardInterrupt:\n    open('guardado.txt', 'w').write('ok')\n    raise SystemExit(2)\n"
    )
    code, timed_out = cm.run_limited(
        [sys.executable, "-c", script], cwd=tmp_path, env={}, log=tmp_path / "log", timeout=1
    )

    assert (code, timed_out) == (2, True)
    assert (tmp_path / "guardado.txt").read_text() == "ok"


@linux_only
def test_run_limited_si_ignora_ctrl_c_lo_mata(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "STOP_GRACE_SECONDS", 1)
    script = "import signal, time\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\ntime.sleep(60)\n"

    code, timed_out = cm.run_limited(
        [sys.executable, "-c", script], cwd=tmp_path, env={}, log=tmp_path / "log", timeout=1
    )

    assert timed_out is True
    assert code == -9  # SIGKILL


def test_programa_completo_entrada_demasiado_grande(monkeypatch):
    """main() con la entrada más grande que el límite (proceso real)."""
    monkeypatch.setattr(cm, "MAX_INPUT_BYTES", 10)
    result = subprocess.run(
        [sys.executable, "-c", "import deadcode_service.coverage_main as m; m.MAX_INPUT_BYTES = 10; m.main()"],
        input=b"x" * 11, capture_output=True, timeout=30,
    )
    assert json.loads(result.stdout) == {
        "ok": False, "error": "El repositorio es demasiado grande (entrada demasiado grande)"
    }
