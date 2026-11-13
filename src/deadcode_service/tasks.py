"""Tareas que ejecuta el worker (no la API).

La API crea la fila en Postgres y anota la tarea en la cola de Redis;
el worker la toma y llama a estas funciones. Deben recibir datos
simples (texto, números) porque viajan guardados en Redis.
"""

import time
import uuid
from datetime import datetime, timezone

from deadcode_service import db

# Nombre de la cola en Redis. La API y el worker deben usar el mismo.
QUEUE_NAME = "analyses"


def analyze_repo(analysis_id: str, repo_url: str) -> dict:
    """Ejecuta un análisis y guarda su estado y resultado en Postgres.

    El análisis en sí es SIMULADO todavía: espera unos segundos y
    devuelve un resultado de prueba. El real (Vulture, coverage.py)
    reemplazará solo la función _run_analysis.
    """
    analysis_uuid = uuid.UUID(analysis_id)
    db.mark_running(analysis_uuid)
    try:
        result = _run_analysis(repo_url)
    except Exception as exc:
        db.mark_failed(analysis_uuid, f"{type(exc).__name__}: {exc}")
        raise  # RQ también registra el fallo en sus logs
    db.mark_finished(analysis_uuid, result)
    return result


def _run_analysis(repo_url: str) -> dict:
    """SIMULADO: no clona ni analiza nada."""
    time.sleep(5)
    return {
        "repo_url": repo_url,
        "dead_code": [],
        "note": "análisis simulado",
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
    }
