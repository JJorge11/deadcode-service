"""Qué librerías hay que descargar para correr los tests de un repo.

Solo LEE el pyproject.toml (con tomllib): nunca ejecuta nada del repo.
Corre dentro del pod de descarga (nivel 2, coverage.py).

Fuentes, en el estándar moderno de Python:
  - [project] dependencies: lo que el código necesita para funcionar.
  - [dependency-groups]: el primer grupo de test que exista ("tests",
    "test", "testing" o "dev"). Puede incluir otros grupos con
    {include-group = "..."}.
  - [build-system] requires: el "empaquetador" (flit_core, setuptools...),
    necesario para instalar el repo en sí.
Más pytest y coverage (nuestra versión fija).

Seguridad: cada requisito se valida con la librería oficial "packaging".
Se RECHAZAN las referencias directas ("pkg @ https://..."): así pip solo
habla con PyPI y un repo no puede mandarlo a visitar otras direcciones.
"""

import tomllib

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from deadcode_service.errors import AnalysisError

TEST_GROUPS = ("tests", "test", "testing", "dev")
# Nuestra versión FIJA de coverage (resultados reproducibles, como Vulture).
COVERAGE_REQUIREMENT = "coverage==7.17.3"
# Si el repo no declara empaquetador, pip usa este (estándar PEP 517).
DEFAULT_BUILD_REQUIRES = ["setuptools>=40.8.0"]
MAX_REQUIREMENTS = 100


class DependencyError(AnalysisError):
    """No se pueden preparar las dependencias (el mensaje lo ve el usuario)."""


def requirements_for_tests(pyproject: bytes) -> list[str]:
    """Lista de requisitos (texto, uno por elemento) para correr los tests."""
    try:
        data = tomllib.loads(pyproject.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise DependencyError("El pyproject.toml del repo no es válido") from exc

    project = _table(data, "project")
    own_name = canonicalize_name(str(project.get("name", "")))
    optional = _table(project, "optional-dependencies")
    groups = _table(data, "dependency-groups")
    build = _table(data, "build-system")

    raw = _string_list(project.get("dependencies", []), "project.dependencies")
    group = next((name for name in TEST_GROUPS if name in groups), None)
    if group:
        raw += _expand_group(groups, group, seen=())
    raw += _string_list(build.get("requires", DEFAULT_BUILD_REQUIRES), "build-system.requires")

    result: dict[str, None] = {}  # dict = lista sin repetidos que respeta el orden
    for text in raw:
        for req in _resolve(text, own_name, optional, seen=()):
            # Nuestra coverage manda: otra versión del repo chocaría con ella.
            if canonicalize_name(req.name) == "coverage":
                continue
            result[str(req)] = None
    names = {canonicalize_name(Requirement(r).name) for r in result}
    if "pytest" not in names:
        result["pytest"] = None
    result[COVERAGE_REQUIREMENT] = None

    if len(result) > MAX_REQUIREMENTS:
        raise DependencyError(f"El repo declara demasiadas dependencias (más de {MAX_REQUIREMENTS})")
    return list(result)


def _table(data: dict, key: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise DependencyError(f"El pyproject.toml del repo no es válido ({key})")
    return value


def _string_list(value: object, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise DependencyError(f"El pyproject.toml del repo no es válido ({where})")
    return list(value)


def _expand_group(groups: dict, name: str, seen: tuple[str, ...]) -> list[str]:
    """Requisitos de un grupo, siguiendo {include-group = "..."}."""
    if name in seen:  # A incluye a B y B incluye a A: cortar el ciclo
        return []
    items = groups.get(name, [])
    if not isinstance(items, list):
        raise DependencyError(f"El pyproject.toml del repo no es válido (grupo {name})")
    result = []
    for item in items:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict) and isinstance(item.get("include-group"), str):
            result += _expand_group(groups, item["include-group"], seen + (name,))
        else:
            raise DependencyError(f"El pyproject.toml del repo no es válido (grupo {name})")
    return result


def _resolve(text: str, own_name: str, optional: dict, seen: tuple[str, ...]) -> list[Requirement]:
    """Valida un requisito. Si el repo se nombra a sí mismo con extras
    (ej. "requests[socks]" dentro de requests), devuelve esos extras."""
    try:
        req = Requirement(text)
    except InvalidRequirement as exc:
        raise DependencyError(f"Dependencia no válida en el pyproject.toml: {text[:100]!r}") from exc
    if req.url:
        raise DependencyError(
            f"Dependencia con URL directa no soportada: {req.name} (solo se descarga desde PyPI)"
        )
    if own_name and canonicalize_name(req.name) == own_name:
        result = []
        for extra in sorted(req.extras):
            if extra in seen:
                continue
            for item in _string_list(optional.get(extra, []), f"optional-dependencies.{extra}"):
                result += _resolve(item, own_name, optional, seen + (extra,))
        return result
    return [req]
