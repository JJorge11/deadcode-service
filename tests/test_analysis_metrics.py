"""Tests de las métricas de los análisis (leídas de Postgres y Redis).

Creamos filas directo en la base de test (cada test empieza con la
tabla vacía) y revisamos lo que publica /metrics.
"""

import uuid

import psycopg
import pytest
from prometheus_client import REGISTRY

from deadcode_service import analysis_metrics, db, fetcher, sandbox
from deadcode_service.coverage_main import INSTALL_TIMEOUT_SECONDS, CoverageError
from deadcode_service.dependencies import MAX_REQUIREMENTS, DependencyError

REPO = "https://github.com/psf/requests"


def _value(name, labels=None):
    return REGISTRY.get_sample_value(name, labels or {})


def _new(status="queued", error=None, seconds=None, dynamic=None):
    """Crea un análisis. seconds = cuánto tardó (finished_at - created_at).
    dynamic = sección "dynamic" del resultado (solo los de nivel 2)."""
    analysis_id = uuid.uuid4()
    db.create_analysis(analysis_id, REPO)
    if status == "running":
        db.mark_running(analysis_id)
    elif status == "finished":
        db.mark_finished(analysis_id, {"dead_code": [], **({"dynamic": dynamic} if dynamic else {})})
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
        return _value("deadcode_analysis_duration_seconds_bucket", {"le": le, "level": "1"})

    assert bucket("10.0") == 0
    assert bucket("15.0") == 1  # el de 12 s
    assert bucket("60.0") == 2  # el de 12 s y el de 40 s
    assert bucket("+Inf") == 2
    assert _value("deadcode_analysis_duration_seconds_sum", {"level": "1"}) == pytest.approx(52, abs=1)


def test_la_duracion_separa_nivel_1_y_nivel_2():
    """Un nivel 2 tarda minutos: mezclado con los de ~7 s, el p95 no diría nada."""
    _new("finished", seconds=8)
    _new("finished", seconds=170, dynamic={"status": "finished", "cross": {}})
    _new("finished", seconds=700, dynamic={"status": "failed", "error": "x"})  # fallar el 2 también cuenta

    def bucket(level, le):
        return _value("deadcode_analysis_duration_seconds_bucket", {"le": le, "level": level})

    assert (bucket("1", "10.0"), bucket("1", "+Inf")) == (1, 1)
    assert (bucket("2", "300.0"), bucket("2", "1200.0"), bucket("2", "+Inf")) == (1, 2, 2)
    assert _value("deadcode_analysis_duration_seconds_sum", {"level": "2"}) == pytest.approx(870, abs=1)


def test_sin_analisis_los_dos_niveles_aparecen_en_cero():
    for level in ("1", "2"):
        assert _value("deadcode_analysis_duration_seconds_count", {"level": level}) == 0


# --- Nivel 2 -----------------------------------------------------------------


def _cross(confirmed, false_positives, not_applicable):
    return {"status": "finished", "coverage_percent": 80.0,
            "cross": {"confirmed": confirmed, "false_positives": false_positives,
                      "not_applicable": not_applicable}}


def test_nivel_2_resultados_y_veredictos_del_cruce():
    _new("finished", dynamic=_cross(6, 5, 126))  # como click
    _new("finished", dynamic=_cross(0, 0, 21))  # como itsdangerous
    _new("finished", dynamic={"status": "failed", "error": "Los tests del repo no pudieron ejecutarse: KeyError: 'X'"})
    _new("finished")  # nivel 1: no cuenta
    _new("failed", error="El repositorio no existe o es privado")  # fallo de nivel 1: no cuenta

    assert _value("deadcode_level2_analyses_total", {"status": "finished"}) == 2
    assert _value("deadcode_level2_analyses_total", {"status": "failed"}) == 1
    assert _value("deadcode_level2_findings_total", {"verdict": "confirmado"}) == 6
    assert _value("deadcode_level2_findings_total", {"verdict": "falso_positivo"}) == 5
    assert _value("deadcode_level2_findings_total", {"verdict": "no_aplica"}) == 147
    assert _value("deadcode_level2_failed_total", {"reason": "tests_no_ejecutables"}) == 1
    assert _value("deadcode_level2_failed_total", {"reason": "sin_pyproject"}) == 0  # aparece aunque sea 0
    # El análisis quedó "finished": el fallo del nivel 2 NO es un fallo del análisis.
    assert _value("deadcode_analyses_failed_total", {"reason": "otro"}) == 0


def test_nivel_2_sin_analisis_todo_en_cero():
    assert _value("deadcode_level2_analyses_total", {"status": "finished"}) == 0
    assert _value("deadcode_level2_findings_total", {"verdict": "confirmado"}) == 0


@pytest.mark.parametrize(
    "error, reason",
    [
        (DependencyError("El repo no tiene pyproject.toml: no se pueden instalar sus dependencias"), "sin_pyproject"),
        (DependencyError("Dependencia con URL directa no soportada: x (solo se descarga desde PyPI)"), "url_directa"),
        (DependencyError(f"El repo declara demasiadas dependencias (más de {MAX_REQUIREMENTS})"), "demasiadas_dependencias"),
        (DependencyError("El pyproject.toml del repo no es válido (grupo tests)"), "pyproject_invalido"),
        (DependencyError("Dependencia no válida en el pyproject.toml: 'x>'"), "pyproject_invalido"),
        (DependencyError("La descarga de dependencias superó el tiempo máximo (240 s)"), "tiempo_agotado"),
        (CoverageError(f"La instalación de dependencias superó el tiempo máximo ({INSTALL_TIMEOUT_SECONDS} s)"), "tiempo_agotado"),
        (CoverageError(f"Los tests del repo superaron el tiempo máximo ({sandbox.COVERAGE_TIMEOUT_SECONDS} s)"), "tiempo_agotado"),
        (CoverageError("Los tests del repo superaron el límite de memoria (1Gi)"), "memoria_agotada"),
        (DependencyError("No se pudieron descargar las dependencias: ERROR: No matching distribution"), "descarga_fallida"),
        (DependencyError("Las dependencias pesan más de 150 MB"), "dependencias_muy_grandes"),
        (CoverageError("No se pudieron instalar las dependencias: ERROR: x"), "instalacion_fallida"),
        (CoverageError("Faltan las dependencias del repo (requirements.txt)"), "instalacion_fallida"),
        (CoverageError("Los tests del repo no pudieron ejecutarse: KeyError: 'POSTGRES_USER'"), "tests_no_ejecutables"),
        (CoverageError("Los tests no llegaron a ejecutar código del repo (sin datos de cobertura)"), "tests_no_ejecutables"),
        (DependencyError("El análisis con tests (nivel 2) solo está disponible en Kubernetes"), "no_disponible"),
        (RuntimeError("Error inesperado (SandboxError)"), "inesperado"),
    ],
)
def test_cada_error_del_nivel_2_tiene_su_motivo(error, reason):
    assert analysis_metrics.failure_reason(str(error), analysis_metrics.LEVEL2_FAILURE_REASONS) == reason


def test_antiguedad_del_pendiente_mas_viejo():
    """Si este número crece sin parar, el worker está atascado o caído."""
    assert _value("deadcode_analyses_oldest_pending_seconds") == 0  # nada pendiente

    _new("queued", seconds=90)
    _new("finished", seconds=500)  # terminado: no cuenta

    assert 90 <= _value("deadcode_analyses_oldest_pending_seconds") < 100


def test_informa_el_tamano_de_la_base():
    """En kind el disco no informa su espacio: se lo preguntamos a Postgres."""
    size = _value("deadcode_postgres_database_bytes")

    assert size > 1_000_000  # una base vacía de Postgres ya ocupa varios MB


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
