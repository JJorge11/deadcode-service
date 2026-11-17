"""Acceso a PostgreSQL: crear la tabla y leer/escribir análisis.

Usamos SQL directo con psycopg (sin ORM) para ver exactamente qué
consultas se ejecutan. Todas las consultas usan parámetros (%s):
psycopg escapa los valores, lo que evita inyección SQL.
"""

import logging
import os
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")

# Estados posibles de un análisis:
#   queued   -> en la cola, esperando un worker
#   running  -> un worker lo está ejecutando
#   finished -> terminó bien (ver "result")
#   failed   -> terminó con error (ver "error")
SCHEMA = """
CREATE TABLE IF NOT EXISTS analyses (
    id          UUID PRIMARY KEY,
    repo_url    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'finished', 'failed')),
    result      JSONB,
    error       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
"""


def connect() -> psycopg.Connection:
    """Abre una conexión. Las filas vuelven como dict ({"id": ..., ...}).

    Usar con "with": al salir hace COMMIT (guarda los cambios), o
    ROLLBACK (los descarta) si hubo un error.
    """
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL no definida")
    return psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=5)


def init_db() -> None:
    """Crea la tabla si no existe. Se llama al arrancar la API."""
    if not DATABASE_URL:
        logger.warning("DATABASE_URL no definida: no se crea la tabla")
        return
    with connect() as conn:
        conn.execute(SCHEMA)


def create_analysis(analysis_id: uuid.UUID, repo_url: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO analyses (id, repo_url) VALUES (%s, %s)",
            (analysis_id, repo_url),
        )


def mark_running(analysis_id: uuid.UUID) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE analyses SET status = 'running' WHERE id = %s",
            (analysis_id,),
        )


def mark_finished(analysis_id: uuid.UUID, result: dict) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE analyses SET status = 'finished', result = %s,"
            " finished_at = now() WHERE id = %s",
            (Jsonb(result), analysis_id),  # Jsonb: dict de Python -> JSONB
        )


def mark_failed(analysis_id: uuid.UUID, error: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE analyses SET status = 'failed', error = %s,"
            " finished_at = now() WHERE id = %s",
            (error, analysis_id),
        )


def get_analysis(analysis_id: uuid.UUID) -> dict | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM analyses WHERE id = %s", (analysis_id,)
        ).fetchone()


def list_analyses(limit: int = 20) -> list[dict]:
    """Los últimos análisis, LIVIANOS: sin la lista de hallazgos.

    Un resultado puede traer cientos de hallazgos; 20 de ellos completos
    serían varios MB solo para pintar una lista. Del resultado se toma
    solo el resumen y el estado del nivel 2 (NULL = nivel 1 o sin terminar).
    """
    with connect() as conn:
        return conn.execute(
            "SELECT id, repo_url, status, error, created_at, finished_at,"
            " result -> 'summary' AS summary,"
            " result -> 'dynamic' ->> 'status' AS dynamic_status"
            " FROM analyses ORDER BY created_at DESC LIMIT %s",
            (limit,),
        ).fetchall()
