"""Tests de la clasificación propia (prioridad, ubicación, categoría).

El repo de ejemplo reproduce cada caso visto al analizar Flask.
"""

import pytest

from deadcode_service.analyzers import python_static
from deadcode_service.analyzers.python_classify import (
    function_parameters,
    location_of,
    visibility_of,
)

REPO_FILES = {
    # Código del producto
    "src/pkg/core.py": '''
import os


class Contexto:
    def __exit__(self, exc_type, exc_value, tb):
        return None


def _path_is_ancestor(path, other):
    return path.startswith(other)


def funcion_publica():
    return 1


def calcular():
    return 2
    print("nunca")


def __getattr__(name):
    raise AttributeError(name)


with Contexto():
    print(calcular())
''',
    # Import que nadie usa, pero en __init__.py (posible re-exportación).
    "src/pkg/__init__.py": "import json\n",
    # Fuera del código del producto
    "tests/test_core.py": "def ayudante_olvidado():\n    pass\n",
    "docs/conf.py": "copyright = '2026'\n",
    "examples/demo.py": "def demo():\n    pass\n",
}


@pytest.fixture
def result(tmp_path):
    for name, content in REPO_FILES.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return python_static.analyze(tmp_path)


def _find(result, name):
    [finding] = [f for f in result["dead_code"] if f["name"] == name]
    return finding


# --- Los casos de Flask -----------------------------------------------


def test_parametro_obligatorio_es_baja_prioridad_aunque_vulture_diga_100(result):
    exc_type = _find(result, "exc_type")

    assert exc_type["confidence"] == 100  # Vulture: "seguro"...
    assert exc_type["category"] == "unused_parameter"
    assert exc_type["priority"] == "low"  # ...pero no es código muerto


def test_privado_sin_usos_es_alta_prioridad_aunque_vulture_diga_60(result):
    finding = _find(result, "_path_is_ancestor")

    assert finding["confidence"] == 60
    assert finding["visibility"] == "private"
    assert finding["priority"] == "high"


def test_publico_sin_usos_es_prioridad_media(result):
    finding = _find(result, "funcion_publica")

    assert finding["visibility"] == "public"
    assert finding["priority"] == "medium"


def test_codigo_inalcanzable_es_alta_prioridad(result):
    [finding] = [f for f in result["dead_code"] if f["category"] == "unreachable_code"]

    assert finding["priority"] == "high"


def test_import_sin_usar_es_alta_prioridad(result):
    assert _find(result, "os")["priority"] == "high"


def test_vulture_ignora_imports_en_init(result):
    """Comportamiento de Vulture (no nuestro): los imports en
    __init__.py suelen ser re-exportaciones y no los reporta."""
    assert not [f for f in result["dead_code"] if f["file"] == "src/pkg/__init__.py"]


def test_dunder_es_baja_prioridad(result):
    finding = _find(result, "__getattr__")

    assert finding["visibility"] == "dunder"
    assert finding["priority"] == "low"


@pytest.mark.parametrize(
    "name, location",
    [("ayudante_olvidado", "tests"), ("copyright", "docs"), ("demo", "examples")],
)
def test_fuera_del_codigo_es_baja_prioridad(result, name, location):
    finding = _find(result, name)

    assert finding["location"] == location
    assert finding["priority"] == "low"


def test_ordena_por_prioridad_y_explica_cada_hallazgo(result):
    priorities = [f["priority"] for f in result["dead_code"]]

    assert priorities == sorted(priorities, key=["high", "medium", "low"].index)
    assert all(f["reason"] for f in result["dead_code"])


def test_resumen_por_prioridad_y_ubicacion(result):
    summary = result["summary"]

    assert summary["total"] == len(result["dead_code"])
    assert sum(summary["by_priority"].values()) == summary["total"]
    assert summary["by_location"]["tests"] == 1
    assert summary["by_location"]["docs"] == 1
    assert summary["by_location"]["examples"] == 1


# --- Funciones auxiliares ---------------------------------------------


@pytest.mark.parametrize(
    "file, expected",
    [
        ("src/app.py", "code"),
        ("app.py", "code"),
        ("tests/test_app.py", "tests"),
        ("src/pkg/test/helpers.py", "tests"),
        ("test_app.py", "tests"),
        ("app_test.py", "tests"),
        ("conftest.py", "tests"),
        ("docs/conf.py", "docs"),
        ("examples/tutorial/app.py", "examples"),
        ("src/latest.py", "code"),  # "test" dentro de una palabra no cuenta
    ],
)
def test_location_of(file, expected):
    assert location_of(file) == expected


@pytest.mark.parametrize(
    "name, expected",
    [("__init__", "dunder"), ("_privado", "private"), ("__muy_privado", "private"), ("publico", "public")],
)
def test_visibility_of(name, expected):
    assert visibility_of(name) == expected


def test_function_parameters_encuentra_todos_los_tipos():
    source = "def f(a, /, b, *args, c, **kwargs):\n    pass\n\ng = lambda x: x\n"

    params = function_parameters(source)

    assert params == {("a", 1), ("b", 1), ("args", 1), ("c", 1), ("kwargs", 1), ("x", 4)}


def test_function_parameters_con_codigo_invalido_no_falla():
    assert function_parameters("def roto(:\n") == set()
