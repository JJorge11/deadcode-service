import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
import redis
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from rq import Queue

from deadcode_service import db, sandbox
from deadcode_service.metrics import setup_metrics
from deadcode_service.tasks import QUEUE_NAME, analyze_repo, job_timeout
from deadcode_service.validation import MAX_URL_LENGTH, normalize_github_url

logger = logging.getLogger("uvicorn.error")

# Dirección de Redis. En Docker Compose viene del docker-compose.yml;
# si no existe (por ejemplo, al correr la API directamente en Windows),
# queda en None. La de Postgres está en db.DATABASE_URL.
REDIS_URL = os.environ.get("REDIS_URL")

# Tiempo máximo (segundos) que esperamos a cada servicio antes de
# darlo por caído. Evita que /health se quede colgado.
TIMEOUT_SECONDS = 2


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Código que corre una vez al arrancar la API (antes del yield)."""
    db.init_db()  # crea la tabla "analyses" si no existe
    yield


app = FastAPI(title="deadcode-service", lifespan=lifespan)
setup_metrics(app)  # GET /metrics para Prometheus (ver metrics.py)


# ---------------------------------------------------------------------
# Salud: ¿responden Postgres y Redis?
# ---------------------------------------------------------------------


def check_postgres() -> str:
    """Abre una conexión y ejecuta SELECT 1 ("¿estás ahí?")."""
    if not db.DATABASE_URL:
        return "error: DATABASE_URL no definida"
    try:
        with psycopg.connect(db.DATABASE_URL, connect_timeout=TIMEOUT_SECONDS) as conn:
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
# Análisis: la API guarda la fila y encola; el worker ejecuta.
# Postgres es la fuente de verdad; Redis es solo la cola.
# ---------------------------------------------------------------------


class AnalysisRequest(BaseModel):
    """Cuerpo del POST. Solo acepta repos de GitHub (ver validation.py).

    El validador corre ANTES de llegar al endpoint: una URL rechazada
    (422) nunca toca la base de datos ni la cola.
    """

    repo_url: str = Field(
        max_length=MAX_URL_LENGTH,
        examples=["https://github.com/psf/requests"],
    )
    # Nivel 2: además corre los tests del repo con coverage (más lento:
    # hasta ~15 min) y confirma o descarta cada hallazgo de Vulture.
    # strict: solo true/false de verdad (no "si", 1, etc.).
    dynamic: bool = Field(
        default=False,
        strict=True,
        description="Nivel 2: correr los tests del repo con coverage.py (solo en Kubernetes)",
    )

    @field_validator("repo_url")
    @classmethod
    def must_be_github_repo(cls, value: str) -> str:
        return normalize_github_url(value)  # devuelve la forma canónica


def get_redis() -> redis.Redis:
    if not REDIS_URL:
        raise HTTPException(status_code=503, detail="REDIS_URL no definida")
    return redis.Redis.from_url(REDIS_URL)


LEVEL2_UNAVAILABLE = (
    "El análisis con tests (nivel 2) solo está disponible en Kubernetes;"
    " aquí se puede pedir solo el nivel 1"
)


@app.get("/config")
def config():
    """Qué ofrece este entorno. La página lo usa para mostrar (o no) la
    opción "con tests"."""
    return {"level2_available": sandbox.level2_available()}


@app.post("/analyses", status_code=202)
def create_analysis(request: AnalysisRequest):
    """Registra un análisis y lo encola. Responde al instante.

    202 = "Aceptado": recibido, pero se procesará después.
    """
    # Nivel 2 donde no se puede (Compose): avisar YA, no después de
    # esperar un análisis que terminaría con el nivel 2 fallido.
    if request.dynamic and not sandbox.level2_available():
        raise HTTPException(status_code=400, detail=LEVEL2_UNAVAILABLE)

    analysis_id = uuid.uuid4()  # identificador único al azar
    repo_url = request.repo_url  # ya validada y normalizada

    # 1) Primero la fila en Postgres: así existe antes de que un
    #    worker rápido intente actualizarla.
    try:
        db.create_analysis(analysis_id, repo_url)
    except psycopg.Error as exc:
        logger.warning("No se pudo guardar el análisis: %s", exc)
        raise HTTPException(status_code=503, detail="Base de datos no disponible")

    # 2) Después el job en Redis, con el mismo id que la fila.
    try:
        queue = Queue(QUEUE_NAME, connection=get_redis())
        queue.enqueue(
            analyze_repo, str(analysis_id), repo_url, request.dynamic,
            job_id=str(analysis_id),
            job_timeout=job_timeout(request.dynamic),  # sin esto RQ corta a los 180 s
        )
    except redis.RedisError as exc:
        logger.warning("No se pudo encolar: %s", exc)
        db.mark_failed(analysis_id, "No se pudo encolar")
        raise HTTPException(status_code=503, detail="Cola no disponible")

    return {"id": analysis_id, "status": "queued", "dynamic": request.dynamic}


@app.get("/analyses")
def list_analyses():
    """Los últimos 20 análisis, del más nuevo al más antiguo.

    Sin los hallazgos (solo el resumen): para verlos, GET /analyses/{id}.
    """
    return db.list_analyses(limit=20)


@app.get("/analyses/{analysis_id}")
def get_analysis(analysis_id: uuid.UUID):
    """Estado y resultado de un análisis. Lee desde Postgres.

    Al declarar el tipo uuid.UUID, FastAPI rechaza (422) ids con
    formato inválido antes de llegar a la base de datos.
    """
    analysis = db.get_analysis(analysis_id)
    if analysis is None:
        raise HTTPException(status_code=404, detail="Análisis no encontrado")
    return analysis


# ---------------------------------------------------------------------
# Página web (dashboard): HTML + JS + CSS que llaman a la API de arriba.
# ---------------------------------------------------------------------

WEB_DIR = Path(__file__).parent / "web"

# CSP (Content-Security-Policy): le dice al navegador qué puede cargar la
# página. Solo nuestros archivos ('self'); ningún script en línea ni de
# otros sitios. Es la 2a defensa contra XSS: los nombres de funciones y
# archivos vienen de repos AJENOS; la 1a es que app.js los inserta
# siempre como texto (textContent), nunca como HTML.
# Va SOLO en la página: /docs carga Swagger desde un CDN y se rompería.
PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self';"
        " img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",  # no "adivinar" tipos: un .js es JS
    "Referrer-Policy": "no-referrer",  # al abrir GitHub no se envía de dónde vienes
}


@app.get("/", include_in_schema=False)
def web_page():
    return FileResponse(WEB_DIR / "index.html", headers=PAGE_HEADERS)


# Rutas fijas (no una carpeta entera): solo se sirve lo que está en esta
# lista, y cada archivo aparece en las métricas con su propio nombre.
@app.get("/app.js", include_in_schema=False)
def web_script():
    return FileResponse(WEB_DIR / "app.js", media_type="text/javascript", headers=PAGE_HEADERS)


@app.get("/app.css", include_in_schema=False)
def web_styles():
    return FileResponse(WEB_DIR / "app.css", media_type="text/css", headers=PAGE_HEADERS)
