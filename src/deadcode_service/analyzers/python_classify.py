"""Clasificación propia de los hallazgos de Vulture.

Problema: la "confianza" de Vulture no dice qué tan probable es que algo
sea código muerto. Su 100% suele ser un parámetro obligatorio sin usar
(ej. exc_type en __exit__) y el código muerto real queda escondido en
el 60%. Por eso agregamos a cada hallazgo:

  location   -> code / tests / docs / examples
  category   -> unused_parameter / unreachable_code / unused_import /
                unused_definition
  visibility -> private (_algo) / dunder (__algo__) / public
  priority   -> high / medium / low, con una explicación ("reason")
"""

import ast
from pathlib import Path, PurePosixPath

TEST_DIRS = {"tests", "test", "testing"}
DOC_DIRS = {"docs", "doc"}
EXAMPLE_DIRS = {"examples", "example", "samples"}

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def location_of(relative_file: str) -> str:
    """¿Dónde está el archivo? Por carpetas y nombre (ej. "tests/x.py")."""
    path = PurePosixPath(relative_file)
    folders = {part.lower() for part in path.parts[:-1]}
    name = path.name.lower()
    if folders & TEST_DIRS or name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
        return "tests"
    if folders & DOC_DIRS:
        return "docs"
    if folders & EXAMPLE_DIRS:
        return "examples"
    return "code"


def visibility_of(name: str) -> str:
    if name.startswith("__") and name.endswith("__"):
        return "dunder"  # Python los usa solo: __init__, __getattr__...
    if name.startswith("_"):
        return "private"  # por convención, solo para uso interno
    return "public"


def function_parameters(source: str) -> set[tuple[str, int]]:
    """Todos los parámetros de funciones como (nombre, línea).

    Usa el AST: el "árbol" que arma Python al leer el código. Así
    distinguimos un parámetro ("def f(moneda)") de una variable normal,
    que Vulture reporta igual ("unused variable").
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    params = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = node.args
            for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]:
                if arg is not None:
                    params.add((arg.arg, arg.lineno))
    return params


def _category(finding: dict, params: set[tuple[str, int]]) -> str:
    if finding["type"] == "unreachable_code":
        return "unreachable_code"
    if finding["type"] == "import":
        return "unused_import"
    if finding["type"] == "variable" and (finding["name"], finding["line"]) in params:
        return "unused_parameter"
    return "unused_definition"


def _priority(location: str, category: str, visibility: str) -> tuple[str, str]:
    """Devuelve (prioridad, explicación). El orden de las reglas importa."""
    if location != "code":
        return "low", f"Está en {location}, no en el código del producto"
    if category == "unused_parameter":
        return "low", "Parámetro sin usar: la firma de la función lo exige, no es código muerto"
    if visibility == "dunder":
        return "low", "Nombre especial (__x__): Python lo usa automáticamente"
    if category == "unreachable_code":
        return "high", "Código inalcanzable: nunca puede ejecutarse"
    if category == "unused_import":
        # (Vulture ya ignora los imports de __init__.py: suelen ser
        # re-exportaciones para otros proyectos.)
        return "high", "Import sin usar"
    if visibility == "private":
        return "high", "Privado (_nombre) y sin usos en todo el repo"
    return "medium", "Público y sin usos en el repo: puede estar muerto o lo usan otros proyectos/frameworks"


def classify(findings: list[dict], repo_dir: Path) -> list[dict]:
    """Agrega location, category, visibility, priority y reason.

    Devuelve los hallazgos ordenados: primero los de prioridad alta.
    """
    params_cache: dict[str, set[tuple[str, int]]] = {}
    classified = []
    for finding in findings:
        file = finding["file"]
        if file not in params_cache:
            source = (repo_dir / file).read_text(encoding="utf-8", errors="ignore")
            params_cache[file] = function_parameters(source)

        location = location_of(file)
        category = _category(finding, params_cache[file])
        visibility = visibility_of(finding["name"])
        priority, reason = _priority(location, category, visibility)

        classified.append(
            {
                **finding,
                "location": location,
                "category": category,
                "visibility": visibility,
                "priority": priority,
                "reason": reason,
            }
        )

    classified.sort(key=lambda f: (PRIORITY_ORDER[f["priority"]], -f["confidence"], f["file"], f["line"]))
    return classified


def summarize(findings: list[dict]) -> dict:
    by_priority = {p: 0 for p in PRIORITY_ORDER}
    by_location = {"code": 0, "tests": 0, "docs": 0, "examples": 0}
    for f in findings:
        by_priority[f["priority"]] += 1
        by_location[f["location"]] += 1
    return {"total": len(findings), "by_priority": by_priority, "by_location": by_location}
