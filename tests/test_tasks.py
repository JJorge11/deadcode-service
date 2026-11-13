"""Tests de la tarea del worker.

El worker real no corre en Windows (RQ usa fork), así que llamamos a
analyze_repo directamente, igual que lo haría el worker.

monkeypatch: herramienta de pytest para reemplazar algo SOLO durante
un test. La usamos para cambiar el análisis simulado (5 s) por uno
instantáneo, o por uno que falla a propósito.
"""

import uuid

import pytest

from deadcode_service import db, tasks

REPO = "https://github.com/psf/requests"


def crear_analisis() -> uuid.UUID:
    analysis_id = uuid.uuid4()
    db.create_analysis(analysis_id, REPO)
    return analysis_id


def test_analisis_exitoso_queda_finished_con_resultado(monkeypatch):
    monkeypatch.setattr(tasks, "_run_analysis", lambda repo_url: {"dead_code": []})
    analysis_id = crear_analisis()

    tasks.analyze_repo(str(analysis_id), REPO)

    row = db.get_analysis(analysis_id)
    assert row["status"] == "finished"
    assert row["result"] == {"dead_code": []}
    assert row["finished_at"] is not None


def test_analisis_que_falla_queda_failed_con_error(monkeypatch):
    def analisis_roto(repo_url):
        raise RuntimeError("repo no encontrado")

    monkeypatch.setattr(tasks, "_run_analysis", analisis_roto)
    analysis_id = crear_analisis()

    # El error debe propagarse (para que RQ lo registre)...
    with pytest.raises(RuntimeError):
        tasks.analyze_repo(str(analysis_id), REPO)

    # ...y además quedar guardado en Postgres.
    row = db.get_analysis(analysis_id)
    assert row["status"] == "failed"
    assert row["error"] == "RuntimeError: repo no encontrado"
    assert row["finished_at"] is not None
