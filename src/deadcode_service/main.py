import logging
import os

import psycopg
import redis
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl
from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job

from deadcode_service.tasks import QUEUE_NAME, analyze_repo

logger = logging.getLogger("uvicorn.error")

app = FastAPI(title="deadcode-service")

# Direcciones de los servicios. En Docker Compose vienen del
# docker-compose.yml; si no existen (por ejemplo, al correr la API
# directamente en Windows), quedan en None.
DATABASE_URL = os.environ.get("DATABASE_URL")
REDIS_URL = os.environ.get("REDIS_URL")

# Tiempo máximo (segundos) que esperamos a cada servicio antes de
# darlo por caído. Evita que /health se quede colgado.
TIMEOUT_SECONDS = 2


def check_postgres() -> str:
    """Abre una conexión y ejecuta SELECT 1 ("¿estás ahí?")."""
    if not DATABASE_URL:
        return "error: DATABASE_URL no definida"
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=TIMEOUT_SECONDS) as conn:
            conn.execute("SELECT 1")
        return "ok"
    except psycopg.Error as exc:
        # El detalle va a los logs; al cliente solo el tipo de error.
        logger.warning("Postgres no responde: %s", exc)
        return f"error: {type(exc).__name__}"


def check_redis() -> str:
    """Envía PING; Redis responde PONG si está vivo."""
    if not REDIS_URL:
        return "error: REDIS_URL no definida"
    try:
        with redis.Redis.from_url(
            REDIS_URL,
            socket_connect_timeout=TIMEOUT_SECONDS,
            socket_timeout=TIMEOUT_SECONDS,
        ) as client:
            client.ping()
        return "ok"
    except redis.RedisError as exc:
        logger.warning("Redis no responde: %s", exc)
        return f"error: {type(exc).__name__}"


@app.get("/health")
def health():
    checks = {"postgres": check_postgres(), "redis": check_redis()}
    all_ok = all(result == "ok" for result in checks.values())

    # 200 = todo bien; 503 = "servicio no disponible". Docker y
    # Kubernetes usan este código para saber si la app está sana.
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={"status": "ok" if all_ok else "error", **checks},
    )


# ---------------------------------------------------------------------
# Análisis: la API solo encola; el worker ejecuta.
# ---------------------------------------------------------------------


class AnalysisRequest(BaseModel):
    """Cuerpo del POST. HttpUrl valida que sea una URL real."""

    repo_url: HttpUrl


def get_redis() -> redis.Redis:
    if not REDIS_URL:
        raise HTTPException(status_code=503, detail="REDIS_URL no definida")
    return redis.Redis.from_url(REDIS_URL)


@app.post("/analyses", status_code=202)
def create_analysis(request: AnalysisRequest):
    """Encola un análisis y responde al instante con su job_id.

    202 = "Aceptado": recibido, pero se procesará después.
    """
    try:
        queue = Queue(QUEUE_NAME, connection=get_redis())
        job = queue.enqueue(analyze_repo, str(request.repo_url))
    except redis.RedisError as exc:
        logger.warning("No se pudo encolar: %s", exc)
        raise HTTPException(status_code=503, detail="Cola no disponible")
    return {"job_id": job.id, "status": job.get_status()}


@app.get("/analyses/{job_id}")
def get_analysis(job_id: str):
    """Consulta el estado de un análisis y, si terminó, su resultado."""
    try:
        job = Job.fetch(job_id, connection=get_redis())
    except NoSuchJobError:
        raise HTTPException(status_code=404, detail="Análisis no encontrado")
    except redis.RedisError as exc:
        logger.warning("No se pudo consultar: %s", exc)
        raise HTTPException(status_code=503, detail="Cola no disponible")
    return {
        "job_id": job.id,
        "status": job.get_status(),  # queued / started / finished / failed
        "result": job.return_value(),  # None hasta que termine
    }
