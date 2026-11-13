import logging
import os

import psycopg
import redis
from fastapi import FastAPI
from fastapi.responses import JSONResponse

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
