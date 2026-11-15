"""Tests de la validación de URLs (protección contra SSRF).

parametrize: ejecuta el MISMO test con cada dato de la lista. Cada
caso aparece como un test separado, así se ve exactamente cuál falla.
"""

import pytest

from deadcode_service import db
from deadcode_service.validation import normalize_github_url

CANONICAL = "https://github.com/psf/requests"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/psf/requests",
        "https://github.com/psf/requests/",  # barra final
        "https://github.com/psf/requests.git",  # sufijo .git
        "https://github.com/psf/requests.git/",
        "  https://github.com/psf/requests  ",  # espacios alrededor
    ],
)
def test_urls_validas_se_normalizan(url):
    assert normalize_github_url(url) == CANONICAL


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/my-org/my_repo.v2",  # guiones, _, puntos
        "https://github.com/a/b",  # nombres de un carácter
    ],
)
def test_nombres_validos_de_github(url):
    assert normalize_github_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        # Otros protocolos (git clone los soporta: ¡peligro!)
        "http://github.com/psf/requests",
        "file:///etc/passwd",
        "ssh://git@github.com/psf/requests",
        "git@github.com:psf/requests.git",
        'ext::sh -c "touch /tmp/hackeado"',
        # Direcciones internas (el corazón de SSRF)
        "http://169.254.169.254/latest/meta-data/",
        "http://2852039166/latest/meta-data/",  # 169.254.169.254 disfrazado
        "http://localhost:6379",
        "http://redis:6379",
        "https://192.168.1.1/admin",
        # Trampas con el nombre del host
        "https://github.com.evil.com/psf/requests",
        "https://evil.com/github.com/psf/requests",
        "https://gist.github.com/psf/requests",
        "https://api.github.com/repos/psf/requests",
        "https://user:clave@github.com/psf/requests",
        "https://github.com@evil.com/psf/requests",
        "https://github.com:8080/psf/requests",
        # Partes extra que no corresponden a un repo
        "https://github.com/psf/requests?x=1",
        "https://github.com/psf/requests#readme",
        "https://github.com/psf/requests/tree/main",
        "https://github.com/psf",
        "https://github.com/",
        # Nombres inválidos
        "https://github.com/psf/..",
        "https://github.com/psf/.",
        "https://github.com/psf/.git",
        "https://github.com/-psf/requests",  # dueño no empieza con guión
        "https://github.com/psf/re%2Fquests",  # caracteres codificados
        # Vacías o enormes
        "",
        "https://github.com/psf/" + "a" * 300,
    ],
)
def test_urls_peligrosas_o_invalidas_se_rechazan(url):
    with pytest.raises(ValueError):
        normalize_github_url(url)


# --- A través de la API ---------------------------------------------


def test_api_rechaza_url_interna_sin_guardar_nada(client):
    response = client.post(
        "/analyses", json={"repo_url": "http://169.254.169.254/latest/meta-data/"}
    )

    assert response.status_code == 422
    assert db.list_analyses() == []


def test_api_guarda_la_url_normalizada(client):
    response = client.post(
        "/analyses", json={"repo_url": "https://github.com/psf/requests.git/"}
    )

    assert response.status_code == 202
    [row] = db.list_analyses()
    assert row["repo_url"] == CANONICAL
