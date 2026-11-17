"""Tests del dashboard web (src/deadcode_service/web/) y de la lista liviana.

La página la dibuja el navegador; aquí comprobamos lo que SÍ se puede
revisar sin navegador: que se sirva, sus cabeceras de seguridad y que
app.js nunca inserte HTML (defensa contra XSS: los nombres de funciones
y archivos vienen de repos ajenos).
"""

import re
import uuid
from pathlib import Path

import pytest

from deadcode_service import db, sandbox

WEB = Path(__file__).resolve().parent.parent / "src" / "deadcode_service" / "web"
REPO = "https://github.com/psf/requests"


def test_la_pagina_se_sirve_con_csp(client):
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<script src="/app.js"' in response.text
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp  # ningún script en línea
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("path, content_type", [("/app.js", "text/javascript"), ("/app.css", "text/css")])
def test_js_y_css_se_sirven(client, path, content_type):
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(content_type)


def test_solo_se_sirven_los_archivos_de_la_lista(client):
    """Nada de "carpeta abierta": otros nombres no existen."""
    for path in ("/index.html", "/web/app.js", "/../pyproject.toml", "/main.py"):
        assert client.get(path).status_code == 404


def test_docs_sigue_sin_csp(client):
    """/docs carga Swagger desde un CDN: con la CSP de la página se rompería."""
    response = client.get("/docs")

    assert response.status_code == 200
    assert "content-security-policy" not in response.headers


def test_la_pagina_no_aparece_en_docs(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert not {"/", "/app.js", "/app.css"} & set(paths)


def test_app_js_nunca_inserta_html():
    """Si alguien usa innerHTML con un nombre de función de un repo
    ajeno, ese repo podría ejecutar código en el navegador (XSS)."""
    code = (WEB / "app.js").read_text(encoding="utf-8")
    # Sin comentarios: el aviso de la cabecera nombra innerHTML a propósito.
    code = re.sub(r"//.*", "", code)
    for dangerous in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert dangerous not in code, dangerous


def test_index_html_sin_scripts_en_linea():
    """La CSP los bloquearía igual, pero así ni siquiera hay tentación."""
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert all("src=" in tag for tag in re.findall(r"<script[^>]*>", html))
    assert not re.search(r"\son[a-z]+\s*=", html)  # nada de onclick="..."
    assert "style=" not in html  # style-src 'self': sin estilos en línea


# --- Lista liviana -------------------------------------------------------------


def test_la_lista_no_trae_los_hallazgos(client):
    """Cada resultado puede traer cientos de hallazgos: la lista solo
    trae el resumen. El detalle sigue completo en GET /analyses/{id}."""
    analysis_id = uuid.uuid4()
    db.create_analysis(analysis_id, REPO)
    summary = {"total": 1, "by_priority": {"high": 1, "medium": 0, "low": 0}}
    db.mark_finished(analysis_id, {
        "dead_code": [{"name": "x"}], "summary": summary,
        "dynamic": {"status": "failed", "error": "sin pyproject"},
    })

    [item] = client.get("/analyses").json()

    assert "result" not in item
    assert item["summary"] == summary
    assert item["dynamic_status"] == "failed"
    assert item["status"] == "finished"
    full = client.get(f"/analyses/{analysis_id}").json()
    assert full["result"]["dead_code"] == [{"name": "x"}]


def test_la_lista_con_pendientes_y_nivel_1(client):
    queued = uuid.uuid4()
    db.create_analysis(queued, REPO)
    level1 = uuid.uuid4()
    db.create_analysis(level1, REPO)
    db.mark_finished(level1, {"dead_code": [], "summary": {"total": 0}})

    items = {item["id"]: item for item in client.get("/analyses").json()}

    assert items[str(queued)]["summary"] is None  # aún sin resultado
    assert items[str(level1)]["dynamic_status"] is None  # nivel 1


# --- ¿Hay nivel 2 en este entorno? ----------------------------------------------


@pytest.mark.parametrize("mode, available", [("kubernetes", True), ("local", True), ("docker", False)])
def test_config_dice_si_hay_nivel_2(client, monkeypatch, mode, available):
    monkeypatch.setattr(sandbox, "SANDBOX_MODE", mode)

    assert client.get("/config").json() == {"level2_available": available}


def test_nivel_2_en_compose_se_rechaza_al_instante(client, redis_client, monkeypatch):
    """Antes se encolaba y, tras esperar, el nivel 2 salía fallido."""
    monkeypatch.setattr(sandbox, "SANDBOX_MODE", "docker")

    response = client.post("/analyses", json={"repo_url": REPO, "dynamic": True})

    assert response.status_code == 400
    assert "solo está disponible en Kubernetes" in response.json()["detail"]
    assert db.list_analyses() == []  # no se guardó nada...
    assert redis_client.keys("rq:job:*") == []  # ...ni se encoló


def test_nivel_1_en_compose_sigue_funcionando(client, redis_client, monkeypatch):
    monkeypatch.setattr(sandbox, "SANDBOX_MODE", "docker")

    assert client.post("/analyses", json={"repo_url": REPO}).status_code == 202
