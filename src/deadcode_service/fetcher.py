"""Descarga segura de repositorios de GitHub.

En vez de `git clone`, descargamos el archivo comprimido (.tar.gz) que
GitHub ofrece para cada repo. Es una sola petición HTTPS, no necesita
git y permite cortar la descarga si es demasiado grande.

Protecciones:
  - La URL de descarga la armamos NOSOTROS con dueño/repo validados.
  - Redirecciones solo hacia dominios de GitHub.
  - Límite de descarga y de tamaño descomprimido (bombas de
    descompresión: archivos chicos que al abrirse ocupan gigas).
  - Solo se extraen archivos .py (lo único que necesita el análisis).
  - tarfile con filter="data": bloquea rutas tramposas (../, absolutas).
"""

import io
import tarfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from deadcode_service.errors import AnalysisError
from deadcode_service.validation import split_github_url

DOWNLOAD_URL = "https://codeload.github.com/{owner}/{repo}/tar.gz/HEAD"
ALLOWED_HOSTS = {"codeload.github.com", "github.com"}
DOWNLOAD_TIMEOUT_SECONDS = 60

MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 50 MB comprimido
MAX_EXTRACTED_BYTES = 300 * 1024 * 1024  # 300 MB de archivos .py
MAX_FILES = 20_000


# --- Errores con mensajes claros (se guardan en la columna "error") --


class RepoNotFoundError(AnalysisError):
    def __init__(self) -> None:
        super().__init__("El repositorio no existe o es privado")


class RepoTooLargeError(AnalysisError):
    def __init__(self, detail: str) -> None:
        super().__init__(f"El repositorio es demasiado grande ({detail})")


class NoPythonFilesError(AnalysisError):
    def __init__(self) -> None:
        super().__init__("El repositorio no contiene archivos Python")


class InvalidArchiveError(AnalysisError):
    def __init__(self) -> None:
        super().__init__("El archivo descargado no es válido")


@dataclass
class DownloadedRepo:
    path: Path  # carpeta raíz del repo ya extraído
    commit: str | None  # SHA del commit analizado (si GitHub lo informa)


# --- Descarga --------------------------------------------------------


class _GitHubOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Sigue redirecciones SOLO si van a un dominio de GitHub."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).hostname not in ALLOWED_HOSTS:
            raise urllib.error.URLError(f"Redirección bloqueada hacia {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_GitHubOnlyRedirects())


def _http_get(url: str) -> bytes:
    """Descarga hasta MAX_DOWNLOAD_BYTES. Separada para poder reemplazarla
    en los tests (así los tests no dependen de internet)."""
    request = urllib.request.Request(url, headers={"User-Agent": "deadcode-service"})
    try:
        with _opener.open(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            # Leemos 1 byte más que el límite: si llega, es demasiado grande.
            data = response.read(MAX_DOWNLOAD_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RepoNotFoundError() from exc
        raise
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise RepoTooLargeError(f"más de {MAX_DOWNLOAD_BYTES // 2**20} MB comprimido")
    return data


# --- Extracción ------------------------------------------------------


def _extract_python_files(data: bytes, dest: Path) -> DownloadedRepo:
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            members = []
            total_bytes = 0
            for member in tar:
                # Solo archivos normales .py: nada de enlaces, carpetas,
                # dispositivos ni otros tipos de archivo.
                if not member.isfile() or not member.name.endswith(".py"):
                    continue
                members.append(member)
                total_bytes += member.size
                if len(members) > MAX_FILES:
                    raise RepoTooLargeError(f"más de {MAX_FILES} archivos Python")
                if total_bytes > MAX_EXTRACTED_BYTES:
                    raise RepoTooLargeError(
                        f"más de {MAX_EXTRACTED_BYTES // 2**20} MB de código Python"
                    )

            if not members:
                raise NoPythonFilesError()

            tar.extractall(dest, members=members, filter="data")
            # GitHub guarda el SHA del commit en la cabecera del archivo.
            commit = tar.pax_headers.get("comment")
    except tarfile.TarError as exc:  # incluye rutas tramposas (FilterError)
        raise InvalidArchiveError() from exc

    # Todo viene dentro de una carpeta raíz, ej. "requests-<sha>/".
    root_folder = members[0].name.split("/")[0]
    return DownloadedRepo(path=dest / root_folder, commit=commit)


def download_repo(repo_url: str, dest: Path) -> DownloadedRepo:
    """Descarga el repo en `dest` (solo sus archivos .py)."""
    owner, repo = split_github_url(repo_url)  # valida de nuevo
    data = _http_get(DOWNLOAD_URL.format(owner=owner, repo=repo))
    return _extract_python_files(data, dest)
