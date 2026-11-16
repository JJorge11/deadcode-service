"""Tests de las métricas de los análisis (leídas de Postgres y Redis).

Creamos filas directo en la base de test (cada test empieza con la
tabla vacía) y revisamos lo que publica /metrics.
"""

import uuid

import psycopg
import pytest
from prometheus_client import REGISTRY

from deadcode_service import analysis_metrics, db, fetcher, sandbox

REPO = "https://github.com/psf/requests"


def _value(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {})


def _new(status="queued", error=None, seconds=None):
    """Crea un análisis. seconds = cuánto tardó (finished_at - created_at)."""
    analysis_id = uuid.uuid4()
    db.create_analysis(analysis_id, REPO)
    if status == "running":
        db.mark_running(analysis_id)
    elif status == "finished":
        db.mark_finished(analysis_id, {"dead_code": []})
    elif status == "failed":
        db.mark_failed(analysis_id, error)
    if seconds is not None:
        with db.connect() as conn:
            conn.execute(
                "UPDATE analyses SET created_at = coalesce(finished_at, now())"
                " - make_interval(secs => %s) WHERE id = %s",
                (seconds, analysis_id),
            )
    return analysis_id


def test_cuenta_analisis_por_estado():
    _new("queued")
    _new("queued")
    _new("finished")
    _new("failed", error="El repositorio no existe o es privado")

    assert _value("deadcode_analyses", {"status": "queued"}) == 2
    assert _value("deadcode_analyses", {"status": "finished"}) == 1
    assert _value("deadcode_analyses", {"status": "failed"}) == 1
    assert _value("deadcode_analyses", {"status": "running"}) == 0  # aparece aunque sea 0


def test_agrupa_los_fallos_en_motivos_fijos():
    """Mensajes con detalles distintos -> un mismo motivo (una sola serie)."""
    _new("failed", error="El repositorio es demasiado grande (más de 50 MB comprimido)")
    _new("failed", error="El repositorio es demasiado grande (más de 20000 archivos Python)")
    _new("failed", error="Error inesperado (SandboxError)")
    _new("failed", error="algo que nunca vimos")

    def failed(reason):
        return _value("deadcode_analyses_failed_total", {"reason": reason})

    assert failed("repo_muy_grande") == 2
    assert failed("inesperado") == 1
    assert failed("otro") == 1
    assert failed("memoria_agotada") == 0


@pytest.mark.parametrize(
    "error",
    [
        fetcher.RepoNotFoundError(),
        fetcher.RepoTooLargeError("más de 50 MB"),
        fetcher.NoPythonFilesError(),
        fetcher.InvalidArchiveError(),
        sandbox.AnalysisTimeoutError(),
        sandbox.AnalysisMemoryError(),
    ],
)
def test_cada_error_conocido_tiene_su_motivo(error):
    """Si alguien cambia el texto de un error, este test avisa (si no,
    sus fallos caerían en "otro" sin que nadie lo note)."""
    assert analysis_metrics.failure_reason(str(error)) != analysis_metrics.OTHER_REASON


def test_mide_la_duracion_de_los_terminados():
    _new("finished", seconds=12)
    _new("finished", seconds=40)
    _new("failed", error="Error inesperado (X)", seconds=3)  # los fallidos no cuentan

    def bucket(le):
        return _value("deadcode_analysis_duration_seconds_bucket", {"le": le})

    assert bucket("10.0") == 0
    assert bucket("15.0") == 1  # el de 12 s
    assert bucket("60.0") == 2  # el de 12 s y el de 40 s
    assert bucket("+Inf") == 2
    assert _value("deadcode_analysis_duration_seconds_sum") == pytest.approx(52, abs=1)


def test_antiguedad_del_pendiente_mas_viejo():
    """Si este número crece sin parar, el worker está atascado o caído."""
    assert _value("deadcode_analyses_oldest_pending_seconds") == 0  # nada pendiente

    _new("queued", seconds=90)
    _new("finished", seconds=500)  # terminado: no cuenta

    assert 90 <= _value("deadcode_analyses_oldest_pending_seconds") < 100


def test_cuenta_la_cola_de_redis(client):
    client.post("/analyses", json={"repo_url": REPO})
    client.post("/analyses", json={"repo_url": REPO})

    assert _value("deadcode_queue_jobs") == 2  # en los tests no hay worker
    assert _value("deadcode_metrics_source_up", {"source": "redis"}) == 1


def test_si_postgres_cae_metrics_sigue_respondiendo(client, monkeypatch):
    def postgres_caido(*args, **kwargs):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", postgres_caido)

    response = client.get("/metrics")

    assert response.status_code == 200
    assert 'deadcode_metrics_source_up{source="postgres"} 0.0' in response.text
    assert 'deadcode_metrics_source_up{source="redis"} 1.0' in response.text
    assert "deadcode_http_requests_total" in response.text  # el resto sigue ahí
