"""Portero del sandbox en Kubernetes: confirma que NO hay red.

Corre como "init container": ANTES que el contenedor del análisis,
dentro del mismo pod (comparten la red). El análisis solo arranca si
este programa termina bien (código 0).

¿Por qué? La red del clúster (kindnet) aplica la NetworkPolicy ~1 s
DESPUÉS de que nace el pod: en ese segundo, el pod SÍ tiene red. Este
programa espera hasta comprobar que la red está bloqueada; si no lo
logra a tiempo, falla y el código ajeno nunca llega a ejecutarse
("fallar cerrado": ante la duda, se bloquea).

Se ejecuta con: python -m deadcode_service.wait_no_network
"""

import os
import socket
import sys
import time

CONNECT_TIMEOUT = 1.0  # segundos por intento de conexión
INTERVAL = 0.5  # pausa entre rondas
MAX_ATTEMPTS = 20  # ~15-30 s como máximo


def targets() -> list[tuple[str, int]]:
    """A dónde intentamos conectarnos (todo debería estar bloqueado).

    - La API de Kubernetes: Kubernetes SIEMPRE le da a cada pod su
      dirección (KUBERNETES_SERVICE_HOST), así que sabemos que existe.
    - Internet (1.1.1.1:443).
    """
    result = [("1.1.1.1", 443)]
    host = os.environ.get("KUBERNETES_SERVICE_HOST")
    if host:
        result.insert(0, (host, int(os.environ.get("KUBERNETES_SERVICE_PORT", "443"))))
    return result


def network_is_blocked(host: str, port: int) -> bool:
    """True solo si NADIE responde (el paquete se pierde o no hay ruta)."""
    try:
        socket.create_connection((host, port), timeout=CONNECT_TIMEOUT).close()
    except ConnectionRefusedError:
        return False  # "rechazada" = alguien contestó = HAY red
    except OSError:
        return True  # tiempo agotado o "sin ruta" = bloqueado
    return False  # conectó = HAY red


def wait_until_blocked(destinations: list[tuple[str, int]]) -> bool:
    """Repite hasta que TODOS los destinos estén bloqueados."""
    for _ in range(MAX_ATTEMPTS):
        if all(network_is_blocked(host, port) for host, port in destinations):
            return True
        time.sleep(INTERVAL)
    return False


def main() -> None:
    destinations = targets()
    if wait_until_blocked(destinations):
        print("Red bloqueada: el análisis puede empezar")
        return
    print(f"ERROR: el sandbox sigue con red ({destinations}); no se analiza", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
