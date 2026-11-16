"""Test del worker REAL de RQ (el mismo que corre en Docker).

Solo en Linux: RQ usa fork, que no existe en Windows. En tu PC este
test aparece como SKIPPED (saltado); en CI (Linux) se ejecuta.
"""

import os
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

from deadcode_service import db, tasks
from deadcode_service.main import app
from deadcode_service.worker import build_worker

REPO = "https://github.com/psf/requests"


@pytest.mark.skipif(sys.platform == "win32", reason="RQ usa fork: solo Linux")
def test_worker_real_procesa_el_analisis_de_punta_a_punta(monkeypatch):
    # Análisis instantáneo. Con fork, el proceso hijo hereda este cambio.
    monkeypatch.setattr(tasks, "_run_analysis", lambda repo_url, dynamic=False: {"dead_code": []})

    # El TestClient usa un hilo interno. Lo cerramos (fin del "with")
    # ANTES de arrancar el worker: hacer fork con hilos vivos puede
    # dejar el proceso hijo bloqueado.
    with TestClient(app) as client:
        analysis_id = client.post("/analyses", json={"repo_url": REPO}).json()["id"]

    # burst=True: procesa lo que hay en la cola y termina (no se queda
    # esperando para siempre como en producción).
    build_worker(os.environ["REDIS_URL"]).work(burst=True)

    row = db.get_analysis(uuid.UUID(analysis_id))
    assert row["status"] == "finished"
    assert row["result"] == {"dead_code": []}
