"""Tests de las métricas de la API (GET /metrics y el middleware).

Los contadores NO vuelven a cero entre tests (viven mientras viva el
proceso), así que medimos la DIFERENCIA antes/después de cada petición.
"""

import uuid

from prometheus_client import REGISTRY


def _requests(method, route, status):
    value = REGISTRY.get_sample_value(
        "deadcode_http_requests_total", {"method": method, "route": route, "status": status}
    )
    return value or 0.0


def _duration_count(method, route):
    value = REGISTRY.get_sample_value(
        "deadcode_http_request_duration_seconds_count", {"method": method, "route": route}
    )
    return value or 0.0


def test_metrics_responde_en_formato_prometheus(client):
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "deadcode_http_requests_total" in response.text
    assert "deadcode_http_request_duration_seconds" in response.text


def test_cuenta_peticiones_y_mide_su_duracion(client):
    before = _requests("GET", "/health", "200")
    before_duration = _duration_count("GET", "/health")

    client.get("/health")

    assert _requests("GET", "/health", "200") == before + 1
    assert _duration_count("GET", "/health") == before_duration + 1


def test_usa_la_ruta_plantilla_no_el_id(client):
    """Con el id real, cada análisis crearía una serie nueva en Prometheus."""
    analysis_id = str(uuid.uuid4())
    before = _requests("GET", "/analyses/{analysis_id}", "404")

    client.get(f"/analyses/{analysis_id}")

    assert _requests("GET", "/analyses/{analysis_id}", "404") == before + 1
    assert analysis_id not in client.get("/metrics").text


def test_urls_inexistentes_comparten_una_sola_etiqueta(client):
    """Un robot probando /wp-admin, /.env... no debe crear series nuevas."""
    before = _requests("GET", "sin_ruta", "404")

    client.get("/wp-admin")
    client.get("/.env")

    assert _requests("GET", "sin_ruta", "404") == before + 2
    text = client.get("/metrics").text
    assert "wp-admin" not in text
    assert ".env" not in text


def test_cuenta_errores_de_validacion(client):
    before = _requests("POST", "/analyses", "422")

    client.post("/analyses", json={"repo_url": "http://169.254.169.254/"})

    assert _requests("POST", "/analyses", "422") == before + 1


def test_no_se_mide_a_si_misma(client):
    """Las visitas de Prometheus a /metrics no deben inflar los números."""
    client.get("/metrics")

    assert REGISTRY.get_sample_value(
        "deadcode_http_requests_total", {"method": "GET", "route": "/metrics", "status": "200"}
    ) is None


def test_metrics_no_aparece_en_la_documentacion(client):
    assert "/metrics" not in client.get("/openapi.json").json()["paths"]
