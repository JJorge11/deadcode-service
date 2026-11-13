"""Tareas que ejecuta el worker (no la API).

La API solo anota la tarea en la cola de Redis; el worker la toma y
llama a estas funciones. Deben ser funciones normales de Python que
reciben y devuelven datos simples (texto, números, dict).
"""

import time
from datetime import datetime, timezone

# Nombre de la cola en Redis. La API y el worker deben usar el mismo.
QUEUE_NAME = "analyses"


def analyze_repo(repo_url: str) -> dict:
    """SIMULADO: todavía no clona ni analiza nada.

    Espera unos segundos para imitar un análisis largo y devuelve un
    resultado de prueba. Sirve para probar el mecanismo cola -> worker.
    El análisis real (Vulture, coverage.py) vendrá en un paso posterior.
    """
    time.sleep(5)
    return {
        "repo_url": repo_url,
        "dead_code": [],
        "note": "análisis simulado",
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
