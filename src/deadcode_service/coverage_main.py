"""Programa que corre DENTRO del pod de tests (nivel 2, coverage.py).

Aquí SÍ se ejecuta código del repo (su instalación y sus tests), por eso
este pod NO tiene red, corre como "nobody" y se destruye al terminar.

Entrada (stdin): un .tar (lo arma pack_input en el worker) con
    repo.tar.gz     el repo, tal como lo descargó el worker
    deps.tar        la salida del pod de descarga (requirements + wheels)
Salida (stdout): un JSON
    {"ok": true, "result": {"tests": {...}, "coverage": {...}}}
    {"ok": false, "error": "mensaje para el usuario"}

Pasos: desempacar -> entorno virtual en /tmp -> instalar las wheels (sin
red: --no-index) -> instalar el repo (editable) -> coverage run -m pytest
-> coverage json -> resumen por archivo (líneas ejecutadas y no).

Cuidados con el código ajeno:
  - Su salida va a archivos de log, NUNCA a nuestro stdout (que es el
    canal de respuesta): no puede "hablar por nosotros" tan fácil.
  - Variables de entorno mínimas, armadas aquí (no hereda nada).
  - Configuración de coverage NUESTRA (--rcfile): ignora la del repo.
  - Plazo: al vencer, primero Ctrl+C (SIGINT) para que coverage guarde
    lo medido; si no obedece, se mata a todo el grupo de procesos.
Límite conocido: el código ajeno corre con el mismo usuario que este
programa y podría alterar el informe. El daño se limita a resultados
falsos DE ESE REPO; no puede salir del pod.

Se ejecuta con: python -m deadcode_service.coverage_main
"""

import io
import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from deadcode_service import downloader_main, fetcher
from deadcode_service.errors import AnalysisError

# Python del SISTEMA de la imagen (el .venv de la app no trae pip/venv).
PYTHON = "/usr/local/bin/python3"
INSTALL_TIMEOUT_SECONDS = 120
TESTS_TIMEOUT_SECONDS = 180
STOP_GRACE_SECONDS = 15  # tras Ctrl+C, cuánto esperar antes de matar
MAX_INPUT_BYTES = fetcher.MAX_DOWNLOAD_BYTES + downloader_main.MAX_WHEELS_BYTES + 10 * 2**20
MAX_SUMMARY_CHARS = 300
PYTEST_RAN = (0, 1)  # códigos de pytest: corrió (todo bien / algunos fallaron)

# Configuración de coverage: SIEMPRE la nuestra (reproducible).
COVERAGE_RC = """\
[run]
source = {source}
data_file = {data_file}
parallel = false
branch = false
"""


class CoverageError(AnalysisError):
    """El nivel 2 no se pudo completar (el mensaje lo ve el usuario)."""


# --- Entrada -----------------------------------------------------------


def pack_input(tarball: bytes, deps: bytes) -> bytes:
    """Lo usa el WORKER: junta el repo y las dependencias en un .tar."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in (("repo.tar.gz", tarball), ("deps.tar", deps)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def unpack_input(data: bytes) -> tuple[bytes, bytes]:
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
            return tar.extractfile("repo.tar.gz").read(), tar.extractfile("deps.tar").read()
    except (tarfile.TarError, KeyError, AttributeError) as exc:
        raise fetcher.InvalidArchiveError() from exc


def extract_deps(deps: bytes, dest: Path) -> tuple[Path, Path]:
    """Extrae requirements.txt y wheels/*.whl (nada más)."""
    wheels = dest / "wheels"
    wheels.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(deps), mode="r:") as tar:
            members = [
                m for m in tar
                if m.isfile() and (
                    m.name == "requirements.txt"
                    or (m.name.startswith("wheels/") and m.name.count("/") == 1 and m.name.endswith(".whl"))
                )
            ]
            tar.extractall(dest, members=members, filter="data")
    except tarfile.TarError as exc:
        raise fetcher.InvalidArchiveError() from exc
    requirements = dest / "requirements.txt"
    if not requirements.is_file():
        raise CoverageError("Faltan las dependencias del repo (requirements.txt)")
    return wheels, requirements


# --- Ejecutar comandos -------------------------------------------------


def clean_env(workdir: Path, venv: Path) -> dict[str, str]:
    """Entorno MÍNIMO para el código ajeno: no hereda variables."""
    home = workdir / "home"
    tmp = workdir / "tmp"
    home.mkdir(exist_ok=True)
    tmp.mkdir(exist_ok=True)
    return {
        "PATH": f"{venv / 'bin'}:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_NO_CACHE_DIR": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }


def run_limited(command: list[str], *, cwd: Path, env: dict, log: Path, timeout: int) -> tuple[int, bool]:
    """Ejecuta `command` con su salida en `log` (no en nuestro stdout).

    Devuelve (código de salida, ¿se venció el plazo?). Al vencer: primero
    SIGINT (como Ctrl+C: pytest termina ordenado y coverage guarda los
    datos); si no obedece, SIGKILL a TODO el grupo (incluye sus hijos).
    """
    with open(log, "ab") as out:
        proc = subprocess.Popen(
            command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True,  # grupo propio: así se puede matar entero
        )
        try:
            return proc.wait(timeout=timeout), False
        except subprocess.TimeoutExpired:
            _signal_group(proc, signal.SIGINT)
            try:
                return proc.wait(timeout=STOP_GRACE_SECONDS), True
            except subprocess.TimeoutExpired:
                _signal_group(proc, signal.SIGKILL)
                return proc.wait(), True


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass  # ya terminó


def _log_lines(log: Path) -> list[str]:
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [line.strip().strip("=").strip() for line in lines]


def _last_line(log: Path, prefix: str = "") -> str:
    """Última línea no vacía del log que empiece con `prefix`."""
    for line in reversed(_log_lines(log)):
        if line and line.startswith(prefix):
            return line[:MAX_SUMMARY_CHARS]
    return ""


# La línea final de pytest: "297 passed in 2.02s", "no tests ran in 0.1s"...
_PYTEST_RESULT = re.compile(r"\b(passed|failed|errors?|no tests ran)\b")


def summarize_tests(log: Path) -> str:
    """Lo más útil para el usuario: el resultado de pytest; si no hay, el
    primer error ("E   KeyError: 'POSTGRES_USER'"); si no, la última línea."""
    lines = _log_lines(log)
    for line in reversed(lines):
        if _PYTEST_RESULT.search(line) and " in " in line:
            return line[:MAX_SUMMARY_CHARS]
    for line in lines:
        if line.startswith("E ") and line[1:].strip():
            return line[1:].strip()[:MAX_SUMMARY_CHARS]
    return _last_line(log)


# --- Pasos -------------------------------------------------------------


def install(workdir: Path, repo: Path, wheels: Path, requirements: Path) -> tuple[Path, bool]:
    """Entorno virtual + dependencias + el repo. Devuelve (venv, ¿repo
    instalado?). Si el repo no se deja instalar, se sigue sin él (los
    tests lo encontrarán por PYTHONPATH)."""
    venv = workdir / "venv"
    env = clean_env(workdir, venv)
    log = workdir / "install.log"
    offline = ["--no-index", "--find-links", str(wheels), "--quiet"]

    steps = [
        [PYTHON, "-m", "venv", str(venv)],
        [str(venv / "bin" / "python"), "-m", "pip", "install", *offline, "--requirement", str(requirements)],
    ]
    for command in steps:
        code, timed_out = run_limited(command, cwd=workdir, env=env, log=log, timeout=INSTALL_TIMEOUT_SECONDS)
        if timed_out:
            raise CoverageError(f"La instalación de dependencias superó el tiempo máximo ({INSTALL_TIMEOUT_SECONDS} s)")
        if code != 0:
            detail = _last_line(log, "ERROR:") or "sin detalle"
            raise CoverageError(f"No se pudieron instalar las dependencias: {detail}")

    # El repo en sí, "editable": sus metadatos existen (click los pide) y
    # el código sigue en su carpeta (coverage mide esa carpeta).
    code, timed_out = run_limited(
        [str(venv / "bin" / "python"), "-m", "pip", "install", *offline, "--no-deps", "--editable", str(repo)],
        cwd=repo, env=env, log=log, timeout=INSTALL_TIMEOUT_SECONDS,
    )
    return venv, code == 0 and not timed_out


def source_dir(repo: Path) -> str:
    """Qué medir: src/ si existe (layout moderno), si no, todo el repo."""
    return "src" if (repo / "src").is_dir() else "."


def run_tests(workdir: Path, repo: Path, venv: Path, installed: bool) -> dict:
    env = clean_env(workdir, venv)
    source = source_dir(repo)
    if not installed:
        env["PYTHONPATH"] = str(repo / source)
    rc = workdir / "coveragerc"
    rc.write_text(COVERAGE_RC.format(source=source, data_file=workdir / ".coverage"), encoding="utf-8")
    log = workdir / "tests.log"
    code, timed_out = run_limited(
        [str(venv / "bin" / "python"), "-m", "coverage", "run", f"--rcfile={rc}",
         "-m", "pytest", "-q", "-p", "no:cacheprovider",
         # Si UN archivo de tests no carga, que corran los demás.
         "--continue-on-collection-errors"],
        cwd=repo, env=env, log=log, timeout=TESTS_TIMEOUT_SECONDS,
    )
    return {
        "exit_code": code,
        "timed_out": timed_out,
        "summary": summarize_tests(log),
        "project_installed": installed,
        "source": source,
    }


def coverage_report(workdir: Path, repo: Path, venv: Path) -> dict:
    """coverage json -> {percent, files: {ruta: {executed, missing}}}."""
    report = workdir / "coverage.json"
    code, _ = run_limited(
        [str(venv / "bin" / "python"), "-m", "coverage", "json", f"--rcfile={workdir / 'coveragerc'}",
         "-o", str(report), "--quiet"],
        cwd=repo, env=clean_env(workdir, venv), log=workdir / "report.log", timeout=60,
    )
    if code != 0 or not report.is_file():
        raise CoverageError("Los tests no llegaron a ejecutar código del repo (sin datos de cobertura)")
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
        files = {}
        for name, info in data["files"].items():
            # Absoluta o relativa (con "..", etc.): se resuelve contra el
            # repo; lo que quede fuera del repo se descarta.
            try:
                path = Path(os.path.normpath(repo / name)).relative_to(repo)
            except ValueError:
                continue
            files[path.as_posix()] = {
                "executed": sorted(info["executed_lines"]),
                "missing": sorted(info["missing_lines"]),
            }
        percent = round(float(data["totals"]["percent_covered"]), 1)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise CoverageError("El informe de cobertura no es válido") from exc
    return {"percent": percent, "files": files}


def process(data: bytes, workdir: Path) -> dict:
    """Todo el nivel 2 dentro del pod. Errores esperados -> ok: false."""
    try:
        tarball, deps = unpack_input(data)
        repo = fetcher.extract_all_files(tarball, workdir / "src").path
        wheels, requirements = extract_deps(deps, workdir / "deps")
        venv, installed = install(workdir, repo, wheels, requirements)
        tests = run_tests(workdir, repo, venv, installed)
        coverage = coverage_report(workdir, repo, venv)
        # pytest: 0 = todo pasó, 1 = algunos fallaron (igual se midió).
        # Otro código y 0% = los tests NO arrancaron: no es "nada se usa".
        if tests["exit_code"] not in PYTEST_RAN and coverage["percent"] == 0:
            raise CoverageError(f"Los tests del repo no pudieron ejecutarse: {tests['summary'] or 'sin detalle'}")
    except AnalysisError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "result": {"tests": tests, "coverage": coverage}}


def main() -> None:
    data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        output = {"ok": False, "error": str(fetcher.RepoTooLargeError("entrada demasiado grande"))}
    else:
        with tempfile.TemporaryDirectory(prefix="deadcode-cov-") as tmp:
            output = process(data, Path(tmp))
    sys.stdout.write(json.dumps(output))


if __name__ == "__main__":
    main()
