"""Programa que corre DENTRO del pod de descarga (nivel 2, coverage.py).

Entrada (stdin):  el .tar.gz del repo, tal como lo descargó el worker.
Salida (stdout):  un .tar (sin comprimir: las wheels ya vienen comprimidas)
    resultado.json      {"ok": true} o {"ok": false, "error": "mensaje"}
    requirements.txt    lo que se pidió a pip (solo si ok)
    wheels/*.whl        las librerías descargadas (solo si ok)

Este pod SÍ tiene internet (para llegar a PyPI) pero NO ejecuta nada del
repo: solo lee su pyproject.toml y descarga WHEELS (paquetes ya armados,
que se instalan copiando archivos). Las librerías que solo existen como
código fuente se rechazan (--only-binary): instalarlas exigiría ejecutar
su setup.py, y eso solo se hace en el pod de tests, SIN red.

Se ejecuta con: python -m deadcode_service.downloader_main
"""

import io
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from deadcode_service import fetcher
from deadcode_service.dependencies import DependencyError, requirements_for_tests
from deadcode_service.errors import AnalysisError

MAX_PYPROJECT_BYTES = 1024 * 1024  # 1 MB: un pyproject.toml normal ocupa ~5 KB
MAX_WHEELS_BYTES = 150 * 1024 * 1024  # 150 MB en total
PIP_TIMEOUT_SECONDS = 180
# pip del Python del SISTEMA de la imagen (el .venv de la app no trae pip).
PIP = ["/usr/local/bin/python3", "-m", "pip"]
# Solo PyPI, fijado aquí: --isolated ignora variables PIP_* y archivos de
# configuración, así nadie puede cambiar a dónde se conecta pip.
PIP_DOWNLOAD_FLAGS = [
    "download",
    "--isolated",
    "--only-binary=:all:",
    "--index-url", "https://pypi.org/simple",
    "--no-cache-dir",
    "--disable-pip-version-check",
    "--progress-bar", "off",
    "--quiet",
]


def read_pyproject(tarball: bytes) -> bytes:
    """Saca SOLO el pyproject.toml de la raíz del repo, sin escribir a disco."""
    try:
        with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
            for member in tar:
                parts = member.name.split("/")
                # GitHub mete todo en una carpeta raíz: "<repo>-<sha>/pyproject.toml".
                if len(parts) == 2 and parts[1] == "pyproject.toml" and member.isfile():
                    if member.size > MAX_PYPROJECT_BYTES:
                        raise DependencyError("El pyproject.toml del repo es demasiado grande")
                    return tar.extractfile(member).read()
    except (tarfile.TarError, EOFError, OSError) as exc:  # OSError: gzip corrupto
        raise fetcher.InvalidArchiveError() from exc
    raise DependencyError(
        "El repo no tiene pyproject.toml: no se pueden instalar sus dependencias para los tests"
    )


def _run_pip(requirements_file: Path, dest: Path) -> subprocess.CompletedProcess:
    """Ejecuta pip download (separado para poder simularlo en los tests)."""
    return subprocess.run(
        [*PIP, *PIP_DOWNLOAD_FLAGS, "--dest", str(dest), "--requirement", str(requirements_file)],
        capture_output=True,
        timeout=PIP_TIMEOUT_SECONDS,
    )


def _pip_error(stderr: bytes) -> str:
    """La primera línea "ERROR: ..." de pip (ej. "No matching distribution
    found for foo"): útil para el usuario y sin detalles internos."""
    for line in stderr.decode("utf-8", errors="replace").splitlines():
        if line.startswith("ERROR:"):
            return line.removeprefix("ERROR:").strip()[:300]
    return "pip falló sin dar el motivo"


def download_wheels(requirements: list[str], workdir: Path) -> list[Path]:
    """Descarga las wheels en workdir/wheels y las devuelve."""
    requirements_file = workdir / "requirements.txt"
    requirements_file.write_text("\n".join(requirements) + "\n", encoding="utf-8")
    dest = workdir / "wheels"
    dest.mkdir()
    try:
        proc = _run_pip(requirements_file, dest)
    except subprocess.TimeoutExpired:
        raise DependencyError(
            f"La descarga de dependencias superó el tiempo máximo ({PIP_TIMEOUT_SECONDS} s)"
        ) from None
    if proc.returncode != 0:
        raise DependencyError(f"No se pudieron descargar las dependencias: {_pip_error(proc.stderr)}")

    wheels = sorted(dest.glob("*.whl"))
    if sum(w.stat().st_size for w in wheels) > MAX_WHEELS_BYTES:
        raise DependencyError(
            f"Las dependencias pesan más de {MAX_WHEELS_BYTES // 2**20} MB"
        )
    return wheels


def _pack(result: dict, requirements: list[str] = (), wheels: list[Path] = ()) -> bytes:
    """Arma el .tar de salida. resultado.json va PRIMERO: quien lo lea
    sabe de entrada si la descarga sirvió."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        files = {"resultado.json": json.dumps(result).encode()}
        if requirements:
            files["requirements.txt"] = ("\n".join(requirements) + "\n").encode()
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for wheel in wheels:
            tar.add(wheel, arcname=f"wheels/{wheel.name}")
    return buffer.getvalue()


def process(tarball: bytes) -> bytes:
    """De .tar.gz del repo a .tar con resultado.json (+ wheels si ok)."""
    try:
        requirements = requirements_for_tests(read_pyproject(tarball))
        with tempfile.TemporaryDirectory(prefix="deadcode-deps-") as tmp:
            wheels = download_wheels(requirements, Path(tmp))
            return _pack({"ok": True}, requirements, wheels)
    except AnalysisError as exc:
        return _pack({"ok": False, "error": str(exc)})


def main() -> None:
    tarball = sys.stdin.buffer.read(fetcher.MAX_DOWNLOAD_BYTES + 1)
    if len(tarball) > fetcher.MAX_DOWNLOAD_BYTES:
        error = fetcher.RepoTooLargeError("entrada demasiado grande")
        sys.stdout.buffer.write(_pack({"ok": False, "error": str(error)}))
        return
    sys.stdout.buffer.write(process(tarball))


if __name__ == "__main__":
    main()
