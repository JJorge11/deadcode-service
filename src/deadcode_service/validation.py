"""Validación de URLs de repositorios (protección contra SSRF).

SSRF: un atacante manda una URL para que NUESTRO servidor se conecte a
lugares internos (credenciales de la nube, Redis, la red privada...).
Como el worker hará `git clone <url>`, la URL debe ser 100% confiable.

Estrategia: LISTA BLANCA. Solo se acepta exactamente
    https://github.com/<dueño>/<repo>
Todo lo demás se rechaza. Una lista negra ("bloquear lo peligroso")
siempre deja escapar algún truco; una lista blanca no.
"""

import re

MAX_URL_LENGTH = 200

# Regla de GitHub para usuarios/organizaciones: letras, números y
# guiones; no empieza con guión; máximo 39 caracteres.
_OWNER = r"[A-Za-z0-9][A-Za-z0-9-]{0,38}"
# Repos: letras, números, punto, guión y guión bajo; máximo 100.
_REPO = r"[A-Za-z0-9._-]{1,100}"

# fullmatch con TODA la URL: no deja espacio para usuario:clave@,
# puertos, otros hosts, ?parámetros, #fragmentos ni rutas extra.
_GITHUB_REPO_URL = re.compile(
    rf"https://github\.com/(?P<owner>{_OWNER})/(?P<repo>{_REPO})/?"
)


def normalize_github_url(url: str) -> str:
    """Devuelve la URL en forma canónica o lanza ValueError.

    Ejemplo: "https://github.com/psf/requests.git/"
          -> "https://github.com/psf/requests"
    """
    if len(url) > MAX_URL_LENGTH:
        raise ValueError(f"La URL supera {MAX_URL_LENGTH} caracteres")

    match = _GITHUB_REPO_URL.fullmatch(url.strip())
    if match is None:
        raise ValueError("Solo se aceptan URLs de la forma https://github.com/<dueño>/<repo>")

    owner = match["owner"]
    repo = match["repo"].removesuffix(".git")

    # "." y ".." sirven para "salir" de una carpeta; usaremos estos
    # nombres para crear carpetas al clonar.
    if repo in ("", ".", ".."):
        raise ValueError("Nombre de repositorio inválido")

    return f"https://github.com/{owner}/{repo}"


def split_github_url(url: str) -> tuple[str, str]:
    """Valida y devuelve (dueño, repo).

    Ejemplo: "https://github.com/psf/requests" -> ("psf", "requests")
    El worker la usa para VOLVER a validar (defensa en profundidad):
    aunque la API ya validó, el job viaja por Redis y no confiamos
    ciegamente en lo que llega desde ahí.
    """
    canonical = normalize_github_url(url)
    owner, repo = canonical.removeprefix("https://github.com/").split("/")
    return owner, repo
