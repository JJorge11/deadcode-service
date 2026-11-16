"""Cruce de los dos "testigos" (nivel 2): Vulture + coverage.py.

  - Vulture (estático) dice: "en el código nadie NOMBRA esto".
  - coverage (dinámico) dice: "durante los tests, el INTERIOR de esta
    función se ejecutó / nunca se ejecutó".

Veredicto por hallazgo (campo "executed"):
  False -> nunca se ejecutó: CONFIRMA a Vulture (medium sube a high).
  True  -> sí se ejecutó: se usa por un camino que Vulture no ve
           (decorador, librería estándar, getattr...): falso positivo
           probable -> baja a low.
  None  -> coverage no puede opinar.

⚠️ La línea del "def" SIEMPRE se ejecuta al importar el módulo (Python
DEFINE la función aunque nadie la llame). Por eso se mira el CUERPO:
de la línea siguiente al def hasta last_line. Y solo funciones, métodos
y propiedades: ejecutar "x = 1" o un import no dice si alguien los LEE.

Solo compara números (líneas): no lee ni ejecuta código del repo.
"""

from deadcode_service.analyzers.python_classify import sort_findings

FUNCTION_TYPES = {"function", "method", "property"}

FALSE_POSITIVE_REASON = (
    "Se ejecutó durante los tests del repo: se usa por un camino que Vulture "
    "no ve (decorador, librería estándar, getattr...). Probable falso positivo."
)
CONFIRMED_SUFFIX = ". Además, los tests del repo nunca lo ejecutaron."


def execution_of(finding: dict, files: dict) -> bool | None:
    """¿Se ejecutó el cuerpo? True / False / None (no se puede saber)."""
    if finding["type"] not in FUNCTION_TYPES:
        return None
    data = files.get(finding["file"])
    if data is None:  # archivo no medido (tests/, docs/, fuera de src/)
        return None
    body = range(finding["line"] + 1, finding["last_line"] + 1)
    executed = set(data["executed"])
    missing = set(data["missing"])
    if any(line in executed for line in body):
        return True
    if any(line in missing for line in body):
        return False
    return None  # cuerpo sin líneas medibles (ej. solo "..." o un docstring)


def cross(findings: list[dict], coverage: dict) -> list[dict]:
    """Agrega "executed" a cada hallazgo y ajusta prioridad y motivo.

    La prioridad original queda en "static_priority" (transparencia: se
    ve qué decía Vulture solo). Devuelve la lista ordenada de nuevo.
    """
    files = coverage.get("files", {})
    result = []
    for finding in findings:
        executed = execution_of(finding, files)
        new = {**finding, "static_priority": finding["priority"], "executed": executed}
        if executed is True:
            new["priority"] = "low"
            new["reason"] = FALSE_POSITIVE_REASON
        elif executed is False:
            if finding["priority"] == "medium":
                new["priority"] = "high"
            new["reason"] = finding["reason"].rstrip(".") + CONFIRMED_SUFFIX
        result.append(new)
    return sort_findings(result)


def summarize_cross(findings: list[dict]) -> dict:
    """Cuántos confirmó, cuántos descartó y sobre cuántos no pudo opinar."""
    values = [f.get("executed") for f in findings]
    return {
        "confirmed": values.count(False),
        "false_positives": values.count(True),
        "not_applicable": values.count(None),
    }
