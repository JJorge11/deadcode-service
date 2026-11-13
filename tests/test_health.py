"""Tests de /health.

Para los casos de falla usamos monkeypatch: reemplazamos (solo durante
el test) la función que conecta por una que lanza el mismo error que
lanzaría un servicio caído. Así probamos que NUESTRO código reacciona
bien, sin apagar Postgres ni Redis de verdad.
"""

import psycopg
import redis


def test_health_ok_cuando_postgres_y_redis_responden(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "postgres": "ok", "redis": "ok"}


def test_health_503_si_postgres_no_responde(client, monkeypatch):
    def postgres_caido(*args, **kwargs):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", postgres_caido)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "error",
        "postgres": "error: OperationalError",  # solo el tipo, sin detalles
        "redis": "ok",
    }


def test_health_503_si_redis_no_responde(client, monkeypatch):
    def redis_caido(self):
        raise redis.ConnectionError("connection refused")

    monkeypatch.setattr(redis.Redis, "ping", redis_caido)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {
        "status": "error",
        "postgres": "ok",
        "redis": "error: ConnectionError",
    }
