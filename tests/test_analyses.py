"""Tests de los endpoints /analyses (la parte de la API)."""

import uuid

import psycopg
import pytest
import redis
from rq import Queue
from rq.job import Job

from deadcode_service import db, tasks
from deadcode_service.tasks import QUEUE_NAME

REPO = "https://github.com/psf/requests"


def test_post_crea_fila_queued_y_encola_job(client, redis_client):
    response = client.post("/analyses", json={"repo_url": REPO})

    assert response.status_code == 202
    analysis_id = response.json()["id"]

    # La fila existe en Postgres, en estado "queued".
    row = db.get_analysis(uuid.UUID(analysis_id))
    assert row["repo_url"] == REPO
    assert row["status"] == "queued"

    # El job está en la cola de Redis, con el MISMO id que la fila...
    queue = Queue(QUEUE_NAME, connection=redis_client)
    assert queue.job_ids == [analysis_id]

    # ...y lleva la función y los argumentos correctos.
    job = Job.fetch(analysis_id, connection=redis_client)
    assert job.func_name == "deadcode_service.tasks.analyze_repo"
    assert list(job.args) == [analysis_id, REPO, False]  # nivel 1 por defecto
    assert job.timeout == tasks.LEVEL1_JOB_TIMEOUT  # no los 180 s de RQ


def test_job_encolado_se_ejecuta_y_completa_el_analisis(client, redis_client, monkeypatch):
    """De punta a punta: la API encola y ejecutamos el job tal como
    quedó guardado en Redis (job.perform() es lo que hace el worker)."""
    monkeypatch.setattr(tasks, "_run_analysis", lambda repo_url, dynamic=False: {"dead_code": []})
    analysis_id = client.post("/analyses", json={"repo_url": REPO}).json()["id"]

    Job.fetch(analysis_id, connection=redis_client).perform()

    response = client.get(f"/analyses/{analysis_id}")
    assert response.json()["status"] == "finished"
    assert response.json()["result"] == {"dead_code": []}


def test_post_rechaza_url_invalida(client):
    response = client.post("/analyses", json={"repo_url": "esto-no-es-url"})

    assert response.status_code == 422
    assert db.list_analyses() == []  # no se guardó nada


def test_post_503_si_postgres_falla_y_no_encola_nada(client, redis_client, monkeypatch):
    def postgres_caido(*args, **kwargs):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(db, "create_analysis", postgres_caido)

    response = client.post("/analyses", json={"repo_url": REPO})

    assert response.status_code == 503
    assert response.json() == {"detail": "Base de datos no disponible"}
    # Sin fila no debe haber job: el worker no tendría dónde escribir.
    assert Queue(QUEUE_NAME, connection=redis_client).job_ids == []


def test_post_503_si_redis_falla_y_marca_la_fila_failed(client, monkeypatch):
    def redis_caido(*args, **kwargs):
        raise redis.ConnectionError("connection refused")

    monkeypatch.setattr(Queue, "enqueue", redis_caido)

    response = client.post("/analyses", json={"repo_url": REPO})

    assert response.status_code == 503
    assert response.json() == {"detail": "Cola no disponible"}
    # La fila ya se había creado: no debe quedar "queued" para siempre.
    [row] = db.list_analyses()
    assert row["status"] == "failed"
    assert row["error"] == "No se pudo encolar"


def test_get_devuelve_el_analisis(client):
    analysis_id = client.post("/analyses", json={"repo_url": REPO}).json()["id"]

    response = client.get(f"/analyses/{analysis_id}")

    assert response.status_code == 200
    assert response.json()["id"] == analysis_id
    assert response.json()["status"] == "queued"


def test_get_id_inexistente_devuelve_404(client):
    response = client.get(f"/analyses/{uuid.uuid4()}")

    assert response.status_code == 404


def test_get_id_con_formato_invalido_devuelve_422(client):
    response = client.get("/analyses/no-es-un-uuid")

    assert response.status_code == 422


def test_listar_devuelve_del_mas_nuevo_al_mas_antiguo(client):
    first = client.post("/analyses", json={"repo_url": REPO}).json()["id"]
    second = client.post("/analyses", json={"repo_url": REPO}).json()["id"]

    ids = [item["id"] for item in client.get("/analyses").json()]

    assert ids == [second, first]


def test_post_nivel_2_encola_con_dynamic_y_mas_plazo(client, redis_client):
    response = client.post("/analyses", json={"repo_url": REPO, "dynamic": True})

    assert response.status_code == 202
    assert response.json()["dynamic"] is True
    job = Job.fetch(response.json()["id"], connection=redis_client)
    assert list(job.args)[2] is True
    assert job.timeout == tasks.LEVEL2_JOB_TIMEOUT


@pytest.mark.parametrize("value", ["si", "true", 1, None])
def test_post_dynamic_solo_acepta_true_o_false(client, value):
    response = client.post("/analyses", json={"repo_url": REPO, "dynamic": value})

    assert response.status_code == 422
    assert db.list_analyses() == []
