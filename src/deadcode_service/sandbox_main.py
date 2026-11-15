"""Programa que corre DENTRO del sandbox (contenedor desechable).

Entrada (stdin):  el .tar.gz del repo, tal como lo descargó el worker.
Salida (stdout):  un JSON con el resultado:
    {"ok": true,  "result": {...}}        -> análisis terminado
    {"ok": false, "error": "mensaje"}     -> error esperado (ej. sin .py)

El sandbox no tiene red ni contraseñas: solo recibe bytes y devuelve
texto. Se ejecuta con: python -m deadcode_service.sandbox_main
"""

import json
import sys
import tempfile
from pathlib import Path

from deadcode_service import fetcher
from deadcode_service.analyzers import python_static
from deadcode_service.errors import AnalysisError


def process(tarball: bytes) -> dict:
    """Extrae y analiza. También se usa en modo "local" (sin Docker)."""
    try:
        with tempfile.TemporaryDirectory(prefix="deadcode-") as tmp:
            repo = fetcher.extract_python_files(tarball, Path(tmp))
            analysis = python_static.analyze(repo.path)
    except AnalysisError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "result": {"commit": repo.commit, **analysis}}


def main() -> None:
    # Leemos como mucho el tamaño máximo de descarga (+1 para detectar
    # si alguien manda más de lo permitido).
    tarball = sys.stdin.buffer.read(fetcher.MAX_DOWNLOAD_BYTES + 1)
    if len(tarball) > fetcher.MAX_DOWNLOAD_BYTES:
        output = {"ok": False, "error": str(fetcher.RepoTooLargeError("entrada demasiado grande"))}
    else:
        output = process(tarball)
    sys.stdout.write(json.dumps(output))


if __name__ == "__main__":
    main()
