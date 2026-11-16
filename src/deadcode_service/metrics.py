"""Métricas de la API para Prometheus.

Prometheus visita GET /metrics cada ~15 s y guarda estos números.
Con ellos Grafana dibuja: peticiones por segundo, % de errores y
cuánto tardan las respuestas.

Tipos usados:
- Counter: solo sube (peticiones atendidas). Prometheus calcula la
  velocidad con rate().
- Histogram: reparte cada duración en "cubetas" (<0,05 s, <0,1 s...).
  Permite calcular, por ejemplo, "el 95% tarda menos de X".

⚠️ Etiquetas (labels): cada combinación distinta crea una serie nueva
en Prometheus. Por eso la ruta es la PLANTILLA (/analyses/{analysis_id})
y nunca la URL real con el id.
"""

import time

from fastapi import FastAPI, Request
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Histogram, generate_latest
from starlette.responses import Response
from starlette.routing import Match

from deadcode_service.analysis_metrics import AnalysisCollector

# Ruta para URLs que no existen (404). Sin esto, un robot que pruebe
# /wp-admin, /.env, etc. crearía miles de series.
UNKNOWN_ROUTE = "sin_ruta"
METRICS_PATH = "/metrics"

REQUESTS = Counter(
    "deadcode_http_requests_total",
    "Peticiones HTTP atendidas por la API.",
    ["method", "route", "status"],
)

DURATION = Histogram(
    "deadcode_http_request_duration_seconds",
    "Tiempo que tarda la API en responder.",
    ["method", "route"],
    # Cubetas pensadas para esta API: /health puede tardar hasta ~4 s
    # (2 s de timeout por Postgres y otros 2 por Redis).
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)


def _route_template(request: Request) -> str:
    """La ruta "plantilla" que atendió la petición, o UNKNOWN_ROUTE."""
    for route in request.app.router.routes:
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            return route.path
    return UNKNOWN_ROUTE


def setup_metrics(app: FastAPI) -> None:
    """Agrega a la app el endpoint /metrics y el middleware que mide."""
    # Métricas de los análisis (leídas de Postgres y Redis en cada visita).
    REGISTRY.register(AnalysisCollector())

    @app.get(METRICS_PATH, include_in_schema=False)  # oculto en /docs
    def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.middleware("http")
    async def measure(request: Request, call_next):
        # Middleware: envuelve CADA petición (antes y después del endpoint).
        if request.url.path == METRICS_PATH:
            return await call_next(request)  # no medir las visitas de Prometheus

        route = _route_template(request)
        start = time.perf_counter()
        status = 500  # si el endpoint lanza un error no manejado, es un 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            DURATION.labels(request.method, route).observe(time.perf_counter() - start)
            REQUESTS.labels(request.method, route, str(status)).inc()
