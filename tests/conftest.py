"""Configuración compartida por todos los tests (pytest la carga sola).

Los tests usan un Postgres y un Redis REALES, pero separados de los
datos de desarrollo:
  - Postgres: base de datos "deadcode_test" (se crea si no existe)
  - Redis:    base número 1 (la app usa la 0)

Requisito local: `docker compose up -d` (Postgres y Redis encendidos).
"""

import os

from dotenv import load_dotenv

# 1) Variables de entorno. IMPORTANTE: deben quedar definidas ANTES de
#    importar deadcode_service, porque sus módulos las leen al cargarse.
load_dotenv()  # local: lee .env. En CI no hay .env; vienen del workflow.

PG_USER = os.environ["POSTGRES_USER"]
PG_PASSWORD = os.environ["POSTGRES_PASSWORD"]
PG_ADMIN_DB = os.environ["POSTGRES_DB"]  # base existente, para crear la de test
# 127.0.0.1 y no "localhost": en Windows, "localhost" intenta primero
# IPv6 (::1), donde Docker no escucha, y pierde ~2 s en CADA conexión.
LOCAL_HOST = "127.0.0.1"
PG_HOST = os.environ.get("POSTGRES_HOST", LOCAL_HOST)
TEST_DB = "deadcode_test"

os.environ["DATABASE_URL"] = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:5432/{TEST_DB}"
os.environ["REDIS_URL"] = f"redis://{os.environ.get('REDIS_HOST', LOCAL_HOST)}:6379/1"

# 2) Recién ahora importamos (después de definir las variables).
import psycopg  # noqa: E402
import pytest  # noqa: E402
import redis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from deadcode_service import db  # noqa: E402
from deadcode_service.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def test_database():
    """Una vez por sesión: crea la base "deadcode_test" y su tabla.

    Un "fixture" es preparación que pytest ejecuta antes de los tests.
    autouse=True: se aplica a todos sin tener que pedirlo.
    """
    admin_url = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:5432/{PG_ADMIN_DB}"
    # CREATE DATABASE no puede ir dentro de una transacción: autocommit.
    with psycopg.connect(admin_url, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    db.init_db()


@pytest.fixture(autouse=True)
def clean_state():
    """Antes de CADA test: tabla y Redis vacíos (tests independientes).

    SEGURO: antes de borrar, pregunta a Postgres y a Redis dónde estamos
    conectados DE VERDAD (no confía en la configuración). Si no es el
    espacio de test, detiene todo para no borrar datos reales.
    """
    with db.connect() as conn:
        current_db = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        if not current_db.endswith("_test"):
            pytest.exit(f"PELIGRO: conectado a '{current_db}', no a una base *_test")
        conn.execute("TRUNCATE analyses")

    redis_test = redis.Redis.from_url(os.environ["REDIS_URL"])
    redis_db = redis_test.connection_pool.connection_kwargs.get("db", 0)
    if redis_db == 0:
        pytest.exit("PELIGRO: Redis apunta a la base 0 (la de la app)")
    redis_test.flushdb()


@pytest.fixture
def client():
    """Cliente para llamar a la API sin levantar un servidor.

    El "with" ejecuta el arranque de la API (lifespan), igual que en real.
    """
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def redis_client():
    return redis.Redis.from_url(os.environ["REDIS_URL"])
