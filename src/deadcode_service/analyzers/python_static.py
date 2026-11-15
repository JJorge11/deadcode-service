"""Análisis estático de Python con Vulture.

Vulture lee el código (sin ejecutarlo) y busca lo que se define pero
nadie usa. Su punto débil son los frameworks: llaman a funciones "por
detrás" (con decoradores como @app.get) y Vulture cree que nadie las
usa. Por eso detectamos qué frameworks usa el repo y le enseñamos a
Vulture sus decoradores y nombres especiales.
"""

import re
from importlib.metadata import version
from pathlib import Path

from vulture import Vulture

from deadcode_service.analyzers.python_classify import classify, summarize

ANALYZER = "vulture"

# Carpetas que no son código del proyecto.
EXCLUDE = ["*/.venv/*", "*/venv/*", "*/site-packages/*", "*/node_modules/*", "*/build/*"]

# Siempre se ignoran: parámetros que muchas veces son obligatorios
# aunque no se usen (ej. cls en un @classmethod, **kwargs en un callback).
ALWAYS_IGNORE_NAMES = ["cls", "args", "kwargs"]

# Reglas por framework: qué import lo delata y qué decoradores/nombres
# usa para llamar a nuestro código "por detrás".
FRAMEWORK_RULES = {
    "fastapi": {
        "decorators": [
            "@*.get", "@*.post", "@*.put", "@*.patch", "@*.delete",
            "@*.api_route", "@*.websocket", "@*.middleware",
            "@*.exception_handler", "@*.on_event",
        ],
        "names": ["lifespan"],
    },
    "flask": {
        "decorators": [
            "@*.route", "@*.get", "@*.post", "@*.put", "@*.patch", "@*.delete",
            "@*.errorhandler", "@*.before_request", "@*.after_request",
            "@*.teardown_request", "@*.context_processor", "@*.cli.command",
        ],
        "names": [],
    },
    "django": {
        "decorators": ["@receiver", "@admin.register", "@register.*"],
        "names": [
            "Meta", "urlpatterns", "app_name", "default_auto_field",
            "get_queryset", "get_context_data", "get_object", "form_valid",
            "save_model", "list_display", "list_filter", "search_fields",
        ],
    },
    "pydantic": {
        "decorators": [
            "@field_validator", "@model_validator", "@validator",
            "@root_validator", "@computed_field", "@field_serializer",
        ],
        "names": ["model_config", "Config"],
    },
    "pytest": {
        "decorators": ["@pytest.fixture", "@fixture", "@pytest.hookimpl"],
        "names": ["test_*", "Test*", "pytest_*", "setup_*", "teardown_*"],
    },
}

_IMPORT_PATTERN = re.compile(
    r"^\s*(?:from|import)\s+(" + "|".join(FRAMEWORK_RULES) + r")\b", re.MULTILINE
)

NOTE = (
    "Ordenado por 'priority' (high, medium, low); 'reason' explica cada "
    "una. 'confidence' es la de Vulture: indica qué tan seguro está de "
    "que el nombre no se usa, NO qué tan probable es que sea código "
    "muerto (su 100% suele ser un parámetro obligatorio)."
)


def _python_files(repo_dir: Path) -> list[Path]:
    return sorted(p for p in repo_dir.rglob("*.py") if p.is_file())


def detect_frameworks(files: list[Path]) -> list[str]:
    """Busca imports de frameworks conocidos en el código."""
    found = set()
    for path in files:
        source = path.read_text(encoding="utf-8", errors="ignore")
        found.update(_IMPORT_PATTERN.findall(source))
    return sorted(found)


def analyze(repo_dir: Path) -> dict:
    """Analiza la carpeta con Vulture. Interfaz común de analizadores."""
    files = _python_files(repo_dir)
    frameworks = detect_frameworks(files)

    ignore_names = list(ALWAYS_IGNORE_NAMES)
    ignore_decorators = []
    for name in frameworks:
        ignore_names += FRAMEWORK_RULES[name]["names"]
        ignore_decorators += FRAMEWORK_RULES[name]["decorators"]

    vulture = Vulture(ignore_names=ignore_names, ignore_decorators=ignore_decorators)
    vulture.scavenge([str(repo_dir)], exclude=EXCLUDE)

    findings = [
        {
            # Ruta relativa al repo ("src/app.py"), con "/" en todo sistema.
            "file": Path(item.filename).relative_to(repo_dir).as_posix(),
            "line": item.first_lineno,
            "last_line": item.last_lineno,
            "type": item.typ,  # function, class, import, variable...
            "name": item.name,
            "confidence": item.confidence,
            "message": item.message,
        }
        for item in vulture.get_unused_code()
    ]
    # Nuestra clasificación (prioridad, ubicación, motivo). Ya ordena.
    findings = classify(findings, repo_dir)

    lines = sum(
        len(p.read_text(encoding="utf-8", errors="ignore").splitlines()) for p in files
    )

    return {
        "analyzer": ANALYZER,
        "analyzer_version": version("vulture"),
        "frameworks_detected": frameworks,
        "files_analyzed": len(files),
        "lines_analyzed": lines,
        "summary": summarize(findings),
        "dead_code": findings,
        "note": NOTE,
    }
