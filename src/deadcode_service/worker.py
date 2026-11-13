"""Worker: escucha la cola de Redis y ejecuta las tareas que llegan.

Se inicia con: python -m deadcode_service.worker
(Solo funciona en Linux, es decir, dentro del contenedor o en CI: RQ
usa "fork", una función de Linux que no existe en Windows.)
"""

import os

import redis
from rq import Queue, Worker

from deadcode_service.tasks import QUEUE_NAME


def build_worker(redis_url: str) -> Worker:
    """Arma el worker conectado a la cola (sin ponerlo a correr).

    Separado de main() para poder probarlo en los tests.
    """
    connection = redis.Redis.from_url(redis_url)
    queue = Queue(QUEUE_NAME, connection=connection)
    return Worker([queue], connection=connection)


def main() -> None:
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        raise SystemExit("REDIS_URL no definida")

    # work() queda en un bucle infinito: espera tareas, las ejecuta y
    # vuelve a esperar. Muestra en los logs cada tarea que procesa.
    build_worker(redis_url).work(logging_level="INFO")


if __name__ == "__main__":
    main()
