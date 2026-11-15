"""Tests de la tarea del worker.

El worker real no corre en Windows (RQ usa fork), así que llamamos a
analyze_repo directamente, igual que lo haría el worker.

monkeypatch: herramienta de pytest para reemplazar algo SOLO durante
un test. Aquí reemplazamos la DESCARGA (para no depender de internet)
o el análisis completo (para provocar errores a propósito).
"""

import uuid

import pytest

from deadcode_service import db, fetcher, tasks
from tests.helpers import make_tarball

REPO = "https://github.com/psf/requests"


def crear_analisis() -> uuid.UUID:
    analysis_id = uuid.uuid4()
    db.create_analysis(analysis_id, REPO)
    return analysis_id


def test_analisis_real_con_vulture_queda_finished(monkeypatch):
    """De punta a punta: descarga (falsa) -> extracción -> Vulture -> Postgres."""
    code = "def usada():\n    return 1\n\n\ndef olvidada():\n    return 2\n\n\nprint(usada())\n"
    monkeypatch.setattr(fetcher, "_http_get", lambda url: make_tarball({"app.py": code}, commit="f00ba4"))
    analysis_id = crear_analisis()

    tasks.analyze_repo(str(analysis_id), REPO)

    row = db.get_analysis(analysis_id)
    assert row["status"] == "finished"
    result = row["result"]
    assert result["repo_url"] == REPO
    assert result["commit"] == "f00ba4"
    assert result["files_analyzed"] == 1
    assert [f["name"] for f in result["dead_code"]] == ["olvidada"]


def test_error_esperado_guarda_mensaje_claro_sin_relanzar(monkeypatch):
    def privado(url):
        raise fetcher.RepoNotFoundError()

    monkeypatch.setattr(fetcher, "_http_get", privado)
    analysis_id = crear_analisis()

    tasks.analyze_repo(str(analysis_id), REPO)  # no lanza: no es un bug

    row = db.get_analysis(analysis_id)
    assert row["status"] == "failed"
    assert row["error"] == "El repositorio no existe o es privado"
    assert row["finished_at"] is not None


def test_error_inesperado_oculta_detalles_y_relanza(monkeypatch):
    def analisis_roto(repo_url):
        raise RuntimeError("detalle interno: /tmp/deadcode-x, clave=123")

    monkeypatch.setattr(tasks, "_run_analysis", analisis_roto)
    analysis_id = crear_analisis()

    # El error debe propagarse (para que RQ lo registre en los logs)...
    with pytest.raises(RuntimeError):
        tasks.analyze_repo(str(analysis_id), REPO)

    # ...pero al usuario solo le mostramos el tipo, sin detalles internos.
    row = db.get_analysis(analysis_id)
    assert row["status"] == "failed"
    assert row["error"] == "Error inesperado (RuntimeError)"


def test_la_carpeta_temporal_se_borra(monkeypatch):
    carpetas = []
    original = fetcher.extract_python_files

    def espiar(data, dest):
        carpetas.append(dest)
        return original(data, dest)

    monkeypatch.setattr(fetcher, "_http_get", lambda url: make_tarball({"app.py": "x = 1"}))
    monkeypatch.setattr(fetcher, "extract_python_files", espiar)

    tasks._run_analysis(REPO)

    [carpeta] = carpetas
    assert not carpeta.exists()
