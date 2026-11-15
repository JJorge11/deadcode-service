"""Tests de la descarga segura (sin internet: la descarga se reemplaza)."""

import io
import tarfile
import urllib.error
import urllib.request

import pytest

from deadcode_service import fetcher
from tests.helpers import make_tarball

REPO = "https://github.com/psf/requests"


@pytest.fixture
def fake_download(monkeypatch):
    """Reemplaza la descarga real. Devuelve una lista donde quedan las
    URLs pedidas, para comprobar a dónde se habría conectado."""
    requested = []

    def install(data: bytes):
        def fake_http_get(url):
            requested.append(url)
            return data

        monkeypatch.setattr(fetcher, "_http_get", fake_http_get)
        return requested

    return install


# --- Descarga y extracción normal -----------------------------------


def test_descarga_desde_codeload_y_extrae_solo_archivos_python(tmp_path, fake_download):
    requested = fake_download(
        make_tarball({"app.py": "x = 1", "pkg/util.py": "y = 2", "README.md": "hola", "logo.png": "..."})
    )

    repo = fetcher.download_repo(REPO, tmp_path)

    assert requested == ["https://codeload.github.com/psf/requests/tar.gz/HEAD"]
    assert repo.path == tmp_path / "repo-abc123"
    assert repo.commit == "abc123"
    extracted = sorted(p.relative_to(repo.path).as_posix() for p in repo.path.rglob("*") if p.is_file())
    assert extracted == ["app.py", "pkg/util.py"]  # sin README ni imágenes


def test_url_invalida_se_rechaza_sin_descargar_nada(tmp_path, fake_download):
    requested = fake_download(make_tarball({"app.py": ""}))

    with pytest.raises(ValueError):
        fetcher.download_repo("http://169.254.169.254/latest", tmp_path)

    assert requested == []  # defensa en profundidad: ni siquiera se conectó


def test_repo_sin_archivos_python(tmp_path, fake_download):
    fake_download(make_tarball({"index.js": "console.log(1)"}))

    with pytest.raises(fetcher.NoPythonFilesError):
        fetcher.download_repo(REPO, tmp_path)


# --- Archivos maliciosos o rotos -------------------------------------


def test_ruta_tramposa_se_bloquea_y_no_escribe_fuera(tmp_path, fake_download):
    dest = tmp_path / "dest"
    dest.mkdir()
    fake_download(make_tarball({"../../evil.py": "import os"}, root=""))

    with pytest.raises(fetcher.InvalidArchiveError):
        fetcher.download_repo(REPO, dest)

    assert not (tmp_path / "evil.py").exists()


def test_enlaces_simbolicos_se_ignoran(tmp_path, fake_download):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        link = tarfile.TarInfo("repo-abc/secreto.py")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tar.addfile(link)
        real = tarfile.TarInfo("repo-abc/app.py")
        tar.addfile(real, io.BytesIO(b""))
    fake_download(buffer.getvalue())

    repo = fetcher.download_repo(REPO, tmp_path)

    assert not (repo.path / "secreto.py").exists()
    assert (repo.path / "app.py").exists()


def test_archivo_corrupto(tmp_path, fake_download):
    fake_download(b"esto no es un tar.gz")

    with pytest.raises(fetcher.InvalidArchiveError):
        fetcher.download_repo(REPO, tmp_path)


# --- Límites de tamaño ------------------------------------------------


def test_demasiados_archivos(tmp_path, fake_download, monkeypatch):
    monkeypatch.setattr(fetcher, "MAX_FILES", 2)
    fake_download(make_tarball({"a.py": "", "b.py": "", "c.py": ""}))

    with pytest.raises(fetcher.RepoTooLargeError):
        fetcher.download_repo(REPO, tmp_path)


def test_demasiados_bytes_descomprimidos(tmp_path, fake_download, monkeypatch):
    monkeypatch.setattr(fetcher, "MAX_EXTRACTED_BYTES", 10)
    fake_download(make_tarball({"grande.py": "x" * 100}))

    with pytest.raises(fetcher.RepoTooLargeError):
        fetcher.download_repo(REPO, tmp_path)


class _FakeResponse:
    def __init__(self, size):
        self.size = size

    def read(self, n):
        return b"x" * min(n, self.size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_descarga_demasiado_grande_se_corta(monkeypatch):
    monkeypatch.setattr(fetcher, "MAX_DOWNLOAD_BYTES", 100)
    monkeypatch.setattr(fetcher._opener, "open", lambda *a, **k: _FakeResponse(1_000))

    with pytest.raises(fetcher.RepoTooLargeError):
        fetcher._http_get("https://codeload.github.com/psf/requests/tar.gz/HEAD")


# --- Respuestas de GitHub y redirecciones ----------------------------


def test_404_significa_repo_inexistente_o_privado(monkeypatch):
    def not_found(*args, **kwargs):
        raise urllib.error.HTTPError("url", 404, "Not Found", None, None)

    monkeypatch.setattr(fetcher._opener, "open", not_found)

    with pytest.raises(fetcher.RepoNotFoundError, match="no existe o es privado"):
        fetcher._http_get("https://codeload.github.com/psf/no-existe/tar.gz/HEAD")


def _redirect(new_url):
    request = urllib.request.Request("https://codeload.github.com/psf/requests/tar.gz/HEAD")
    return fetcher._GitHubOnlyRedirects().redirect_request(request, None, 302, "Found", {}, new_url)


def test_redireccion_a_github_se_permite():
    assert _redirect("https://codeload.github.com/psf/requests-nuevo/tar.gz/HEAD") is not None


@pytest.mark.parametrize(
    "new_url",
    ["http://169.254.169.254/latest/meta-data/", "https://evil.com/x.tar.gz", "https://github.com.evil.com/x"],
)
def test_redireccion_fuera_de_github_se_bloquea(new_url):
    with pytest.raises(urllib.error.URLError, match="bloqueada"):
        _redirect(new_url)
