"""Métricas de los ANÁLISIS, leídas de Postgres y Redis.

¿Por qué no contar dentro del worker? RQ hace "fork": cada análisis
corre en una COPIA del proceso que desaparece al terminar, y lo que esa
copia cuente se pierde con ella. En cambio, todo lo que queremos saber
ya está guardado en Postgres (estado, error, horas) y en Redis (la
cola). Así que cuando Prometheus visita GET /metrics de la API,
preguntamos ahí y respondemos con los números del momento.

Consecuencia: las 2 réplicas de la API reportan el MISMO número. En
Grafana usar max(...), no sum(...) (sum contaría doble).
"""

import logging
import os

import psycopg
import redis
from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
)
from rq import Queue

from deadcode_service import db
from deadcode_service.tasks import QUEUE_NAME

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL")
TIMEOUT_SECONDS = 2

STATUSES = ("queued", "running", "finished", "failed")

# Cubetas (segundos) para la duración total: espera en cola + análisis.
# Nivel 1: ~7 s (máximo del sandbox 120 s + arranque). Nivel 2: ~2-6 min
# (3 pods; su trabajo en RQ puede durar hasta 1200 s).
DURATION_BUCKETS = (5, 10, 15, 30, 60, 120, 300, 600, 1200)
LEVELS = ("1", "2")

# Mensaje de error -> motivo FIJO. Los mensajes pueden traer detalles
# variables ("demasiado grande (más de 50 MB...)"); como etiqueta, cada
# variante sería una serie nueva. Se busca un fragmento del mensaje.
FAILURE_REASONS = (
    ("El repositorio no existe", "repo_no_existe"),
    ("demasiado grande", "repo_muy_grande"),
    ("no contiene archivos Python", "sin_python"),
    ("no es válido", "archivo_invalido"),
    ("tiempo máximo", "tiempo_agotado"),
    ("límite de memoria", "memoria_agotada"),
    ("Error inesperado", "inesperado"),
    ("No se pudo encolar", "cola_no_disponible"),
)
OTHER_REASON = "otro"


# Lo mismo para el NIVEL 2 (descarga + tests). Ojo: si el nivel 2 falla,
# el análisis igual queda "finished" (con lo de Vulture), así que estos
# fallos NO aparecen en deadcode_analyses_failed. Se busca en ORDEN: el
# primer fragmento que aparezca gana ("tiempo máximo" antes que
# "instalación", para que un plazo agotado al instalar cuente como plazo).
LEVEL2_FAILURE_REASONS = (
    ("no tiene pyproject.toml", "sin_pyproject"),
    ("URL directa", "url_directa"),
    ("demasiadas dependencias", "demasiadas_dependencias"),
    ("pyproject.toml", "pyproject_invalido"),
    ("Dependencia no válida", "pyproject_invalido"),
    ("tiempo máximo", "tiempo_agotado"),
    ("límite de memoria", "memoria_agotada"),
    ("No se pudieron descargar", "descarga_fallida"),
    ("pesan más de", "dependencias_muy_grandes"),
    ("instalar las dependencias", "instalacion_fallida"),
    ("Faltan las dependencias", "instalacion_fallida"),
    ("no pudieron ejecutarse", "tests_no_ejecutables"),
    ("no llegaron a ejecutar", "tests_no_ejecutables"),
    ("solo está disponible en Kubernetes", "no_disponible"),
    ("Error inesperado", "inesperado"),
)
LEVEL2_VERDICTS = (
    ("confirmed", "sin_ejecutar"),  # Vulture "no se usa" + nunca se ejecutó (NO prueba que esté muerto)
    ("false_positives", "falso_positivo"),  # Vulture "no se usa" + sí se ejecutó
    ("not_applicable", "no_aplica"),  # coverage no puede opinar (import, variable...)
)


def failure_reason(error: str | None, reasons=FAILURE_REASONS) -> str:
    """Convierte el mensaje de error guardado en un motivo fijo."""
    for fragment, reason in reasons:
        if fragment in (error or ""):
            return reason
    return OTHER_REASON


def _reason_counts(rows, reasons) -> dict:
    """Filas (error, n) -> {motivo: total}, con TODOS los motivos (aunque 0)."""
    counts = {reason: 0 for _, reason in reasons}
    counts[OTHER_REASON] = 0
    for row in rows:
        counts[failure_reason(row["error"], reasons)] += row["n"]
    return counts


# Las columnas le_<n> se arman con los números FIJOS de arriba (no con
# datos del usuario), así que no hay riesgo de inyección SQL.
# El nivel no tiene columna propia: un análisis es de nivel 2 si su
# resultado trae la sección "dynamic" (haya salido bien o no).
_DURATION_SQL = (
    "SELECT level, "
    + ", ".join(f"count(*) FILTER (WHERE d <= {b}) AS le_{b}" for b in DURATION_BUCKETS)
    + ", count(*) AS total, coalesce(sum(d), 0) AS total_seconds"
    " FROM (SELECT extract(epoch FROM finished_at - created_at) AS d,"
    "              CASE WHEN result -> 'dynamic' IS NULL THEN '1' ELSE '2' END AS level"
    "       FROM analyses WHERE status = 'finished' AND finished_at IS NOT NULL) AS t"
    " GROUP BY level"
)

_LEVEL2_SQL = (
    "SELECT result -> 'dynamic' ->> 'status' AS status, result -> 'dynamic' ->> 'error' AS error,"
    " count(*) AS n"
    + "".join(f", coalesce(sum((result -> 'dynamic' -> 'cross' ->> '{key}')::int), 0) AS {key}"
              for key, _ in LEVEL2_VERDICTS)
    + " FROM analyses WHERE status = 'finished' AND result -> 'dynamic' IS NOT NULL"
    " GROUP BY 1, 2"
)


def _postgres_metrics():
    with db.connect() as conn:
        by_status = {row["status"]: row["n"] for row in conn.execute(
            "SELECT status, count(*) AS n FROM analyses GROUP BY status"
        )}
        errors = conn.execute(
            "SELECT error, count(*) AS n FROM analyses WHERE status = 'failed' GROUP BY error"
        ).fetchall()
        durations = {row["level"]: row for row in conn.execute(_DURATION_SQL)}
        level2 = conn.execute(_LEVEL2_SQL).fetchall()
        oldest = conn.execute(
            "SELECT coalesce(extract(epoch FROM now() - min(created_at)), 0) AS s"
            " FROM analyses WHERE status IN ('queued', 'running')"
        ).fetchone()["s"]
        # En kind el disco (local-path) NO informa su espacio libre: no hay
        # kubelet_volume_stats. Le preguntamos a Postgres cuánto ocupa la
        # base. Aproximado: no incluye el WAL ni otros archivos internos.
        database_bytes = conn.execute(
            "SELECT pg_database_size(current_database()) AS bytes"
        ).fetchone()["bytes"]

    analyses = GaugeMetricFamily(
        "deadcode_analyses", "Análisis guardados, por estado.", labels=["status"]
    )
    for status in STATUSES:  # todos, aunque sea 0: los gráficos no quedan con huecos
        analyses.add_metric([status], by_status.get(status, 0))

    failures = _reason_counts(errors, FAILURE_REASONS)
    # Counter: las filas no se borran, así que el total solo sube.
    failed = CounterMetricFamily(
        "deadcode_analyses_failed", "Análisis fallidos, por motivo.", labels=["reason"]
    )
    for reason, count in failures.items():
        failed.add_metric([reason], count)

    histogram = HistogramMetricFamily(
        "deadcode_analysis_duration_seconds",
        "Tiempo total (espera en cola + análisis) de los análisis terminados bien, por nivel.",
        labels=["level"],
    )
    for level in LEVELS:
        row = durations.get(level)
        histogram.add_metric(
            [level],
            buckets=[(str(float(b)), row[f"le_{b}"] if row else 0) for b in DURATION_BUCKETS]
            + [("+Inf", row["total"] if row else 0)],
            sum_value=float(row["total_seconds"]) if row else 0.0,
        )

    pending = GaugeMetricFamily(
        "deadcode_analyses_oldest_pending_seconds",
        "Segundos que lleva esperando el análisis pendiente más antiguo (0 = ninguno).",
        value=float(oldest),
    )

    size = GaugeMetricFamily(
        "deadcode_postgres_database_bytes",
        "Tamaño de la base de datos (pg_database_size; sin el WAL).",
        value=float(database_bytes),
    )
    return [analyses, failed, histogram, pending, size, *_level2_metrics(level2)]


def _level2_metrics(rows):
    """Nivel 2: cuántos salieron bien/mal, por qué fallaron y qué dijo el cruce."""
    results = CounterMetricFamily(
        "deadcode_level2_analyses",
        "Análisis de nivel 2 (con tests), por resultado del nivel 2.",
        labels=["status"],
    )
    for status in ("finished", "failed"):
        results.add_metric([status], sum(r["n"] for r in rows if r["status"] == status))

    failed = CounterMetricFamily(
        "deadcode_level2_failed",
        "Fallos del nivel 2, por motivo (el análisis igual entregó lo de Vulture).",
        labels=["reason"],
    )
    reasons = _reason_counts([r for r in rows if r["status"] == "failed"], LEVEL2_FAILURE_REASONS)
    for reason, count in reasons.items():
        failed.add_metric([reason], count)

    verdicts = CounterMetricFamily(
        "deadcode_level2_findings",
        "Hallazgos de Vulture según el cruce con coverage (suma de todos los análisis de nivel 2).",
        labels=["verdict"],
    )
    for key, verdict in LEVEL2_VERDICTS:
        verdicts.add_metric([verdict], sum(r[key] for r in rows))
    return [results, failed, verdicts]


def _redis_metrics():
    if not REDIS_URL:
        raise redis.ConnectionError("REDIS_URL no definida")
    with redis.Redis.from_url(
        REDIS_URL, socket_connect_timeout=TIMEOUT_SECONDS, socket_timeout=TIMEOUT_SECONDS
    ) as client:
        waiting = Queue(QUEUE_NAME, connection=client).count
    return [GaugeMetricFamily(
        "deadcode_queue_jobs", "Análisis esperando en la cola de Redis.", value=waiting
    )]


class AnalysisCollector:
    """Prometheus llama a collect() en cada visita a /metrics."""

    def describe(self):
        # Sin esto, prometheus_client llamaría a collect() al registrarlo
        # (al importar la app), cuando Postgres quizás aún no responde.
        return []

    def collect(self):
        up = GaugeMetricFamily(
            "deadcode_metrics_source_up",
            "1 si se pudo leer la fuente de estas métricas, 0 si no.",
            labels=["source"],
        )
        # Si Postgres o Redis fallan, /metrics responde igual (con
        # source_up = 0): así Prometheus sigue recibiendo el resto.
        for source, read, errors in (
            ("postgres", _postgres_metrics, (psycopg.Error, RuntimeError)),
            ("redis", _redis_metrics, (redis.RedisError,)),
        ):
            try:
                yield from read()
                up.add_metric([source], 1)
            except errors as exc:
                logger.warning("Métricas: no se pudo leer %s: %s", source, exc)
                up.add_metric([source], 0)
        yield up
