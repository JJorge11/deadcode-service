"""Tareas que ejecuta el worker (no la API).

La API crea la fila en Postgres y anota la tarea en la cola de Redis;
el worker la toma y llama a estas funciones. Deben recibir datos
simples (texto, números) porque viajan guardados en Redis.
"""

import logging
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from deadcode_service import db, fetcher
from deadcode_service.analyzers import python_static
from deadcode_service.errors import AnalysisError

logger = logging.getLogger(__name__)

# Nombre de la cola en Redis. La API y el worker deben usar el mismo.
QUEUE_NAME = "analyses"


def analyze_repo(analysis_id: str, repo_url: str) -> dict | None:
    """Ejecuta un análisis y guarda su estado y resultado en Postgres."""
    analysis_uuid = uuid.UUID(analysis_id)
    db.mark_running(analysis_uuid)
    try:
        result = _run_analysis(repo_url)
    except AnalysisError as exc:
        # Error esperado (repo privado, muy grande...): mensaje claro
        # para el usuario. No es un bug, así que no relanzamos.
        logger.warning("Análisis %s falló: %s", analysis_id, exc)
        db.mark_failed(analysis_uuid, str(exc))
        return None
    except Exception as exc:
        # Error inesperado (bug): al usuario solo el tipo; el detalle
        # queda en los logs porque relanzamos y RQ lo registra.
        db.mark_failed(analysis_uuid, f"Error inesperado ({type(exc).__name__})")
        raise
    db.mark_finished(analysis_uuid, result)
    return result


def _run_analysis(repo_url: str) -> dict:
    """Descarga el repo en una carpeta temporal y lo analiza.

    La carpeta se borra sola al salir del "with", aunque haya errores.
    """
    with tempfile.TemporaryDirectory(prefix="deadcode-") as tmp:
        repo = fetcher.download_repo(repo_url, Path(tmp))
        analysis = python_static.analyze(repo.path)

    return {
        "repo_url": repo_url,
        "commit": repo.commit,
        **analysis,
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
    }
