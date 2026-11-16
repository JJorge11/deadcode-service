"""Tareas que ejecuta el worker (no la API).

La API crea la fila en Postgres y anota la tarea en la cola de Redis;
el worker la toma y llama a estas funciones. Deben recibir datos
simples (texto, números) porque viajan guardados en Redis.
"""

import logging
import uuid
from datetime import datetime, timezone

from deadcode_service import db, fetcher, sandbox
from deadcode_service.analyzers.python_classify import summarize
from deadcode_service.analyzers.python_cross import cross, summarize_cross
from deadcode_service.errors import AnalysisError

logger = logging.getLogger(__name__)

# Nombre de la cola en Redis. La API y el worker deben usar el mismo.
QUEUE_NAME = "analyses"

# Plazo de cada trabajo en la cola (RQ lo corta al vencer; sin esto usa
# 180 s). Nivel 1: descarga (60 s) + pod de análisis (150 s) + margen.
# Nivel 2: además el pod de descarga (~270 s) y el de tests (~390 s).
LEVEL1_JOB_TIMEOUT = 300
LEVEL2_JOB_TIMEOUT = 1200


def job_timeout(dynamic: bool) -> int:
    return LEVEL2_JOB_TIMEOUT if dynamic else LEVEL1_JOB_TIMEOUT


def analyze_repo(analysis_id: str, repo_url: str, dynamic: bool = False) -> dict | None:
    """Ejecuta un análisis y guarda su estado y resultado en Postgres.

    dynamic=True agrega el nivel 2 (tests del repo + coverage). Tiene
    valor por defecto: los trabajos que ya estaban en la cola (con solo
    2 argumentos) siguen funcionando.
    """
    analysis_uuid = uuid.UUID(analysis_id)
    db.mark_running(analysis_uuid)
    try:
        result = _run_analysis(repo_url, dynamic=dynamic)
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


def _run_analysis(repo_url: str, dynamic: bool = False) -> dict:
    """El worker descarga (código confiable: el nuestro) y el SANDBOX
    abre y analiza el código ajeno, aislado (ver sandbox.py)."""
    tarball = fetcher.download_tarball(repo_url)
    analysis = sandbox.run(tarball)
    if dynamic:
        analysis = _add_dynamic(tarball, analysis)

    return {
        "repo_url": repo_url,
        **analysis,
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
    }


def _add_dynamic(tarball: bytes, analysis: dict) -> dict:
    """Nivel 2: pod de descarga -> pod de tests -> cruce con Vulture.

    Si falla, el análisis IGUAL termina con lo de Vulture (nivel 1) y en
    "dynamic" queda el motivo. El nivel 2 nunca hace perder el nivel 1.
    """
    try:
        deps = sandbox.download_dependencies(tarball)
        coverage = sandbox.run_coverage(tarball, deps)
    except AnalysisError as exc:
        # Esperado (sin pyproject.toml, tests que no arrancan, plazo...).
        return {**analysis, "dynamic": {"status": "failed", "error": str(exc)}}
    except Exception as exc:  # noqa: BLE001 - un bug del nivel 2 no tumba el nivel 1
        logger.exception("El nivel 2 falló de forma inesperada")
        return {**analysis, "dynamic": {"status": "failed", "error": f"Error inesperado ({type(exc).__name__})"}}

    findings = cross(analysis["dead_code"], coverage["coverage"])
    tests = coverage["tests"]
    return {
        **analysis,
        "dead_code": findings,
        "summary": summarize(findings),  # las prioridades cambiaron
        "dynamic": {
            "status": "finished",
            "coverage_percent": coverage["coverage"]["percent"],
            "tests": {
                "summary": tests["summary"],
                "exit_code": tests["exit_code"],
                "timed_out": tests["timed_out"],
            },
            "cross": summarize_cross(findings),
        },
    }
