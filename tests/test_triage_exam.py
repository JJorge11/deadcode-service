"""Tests del armado del examen de la etapa 8 (scripts/build_triage_exam.py).
Solo las partes puras: sin red ni código ajeno."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "build_triage_exam.py"


def _load():
    spec = importlib.util.spec_from_file_location("build_triage_exam", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


exam = _load()

CODE = """\
import os


class Cache:
    \"\"\"Una caché.\"\"\"

    @property
    @something
    def size(self):
        return 1

    def clear(self):
        def inner():
            return 2
        return inner()


def helper(x):
    return x + 1
""".splitlines()


@pytest.mark.parametrize("executed, label", [
    (True, "falso_positivo"), (False, "muerto_probable"), (None, None),
])
def test_etiqueta(executed, label):
    assert exam.label_of({"executed": executed}) == label


def test_etiqueta_sin_campo_executed():
    """Análisis viejos (sin cruce) no deben colarse en el examen."""
    assert exam.label_of({}) is None


def test_recorte_incluye_decoradores_y_numeros_de_linea():
    code = exam.cut_code(CODE, line=9, last_line=10)

    assert code.splitlines() == [
        "    7|     @property",
        "    8|     @something",
        "    9|     def size(self):",
        "   10|         return 1",
    ]


def test_recorte_de_funcion_larga():
    lines = ["def larga():"] + [f"    x{i} = {i}" for i in range(100)]

    code = exam.cut_code(lines, line=1, last_line=101).splitlines()

    assert len(code) == exam.MAX_CODE_LINES + 1
    assert code[-1].endswith(f"({101 - exam.MAX_CODE_LINES} líneas más)")


@pytest.mark.parametrize("line, expected", [
    (9, "Cache"),     # método con decoradores
    (12, "Cache"),    # método
    (13, "Cache"),    # función dentro de un método: sigue subiendo
    (18, None),       # función suelta
])
def test_clase_que_contiene(line, expected):
    assert exam.enclosing_class(CODE, line) == expected


def test_division_por_repo_sin_mezclar():
    counts = {f"dueño/repo{i}": 10 for i in range(10)}

    final = exam.split_by_repo(counts, share=0.3)

    taken = sum(counts[r] for r in final)
    assert 30 <= taken < 40  # ~30%, en repos enteros
    assert final == exam.split_by_repo(dict(reversed(list(counts.items()))), share=0.3)  # determinista


def test_el_caso_no_sopla_la_respuesta():
    """Lo que dice el cruce (prioridad nueva, motivo con "los tests...",
    executed) NO va en el caso: la IA debe opinar sin saberlo."""
    finding = {
        "file": "pkg/cache.py", "line": 12, "last_line": 15, "name": "clear",
        "type": "method", "executed": False, "priority": "high", "static_priority": "medium",
        "reason": "Público sin usos. Además, los tests del repo nunca lo ejecutaron.",
        "message": "unused method 'clear' (60% confidence)", "confidence": 60,
        "visibility": "public", "location": "code", "category": "method",
    }

    case = exam.make_case("dueño/repo", "a" * 40, finding, CODE)

    assert case["label"] == "muerto_probable"
    assert case["static_priority"] == "medium"
    assert case["class"] == "Cache"
    for leak in ("executed", "priority", "reason", "message"):
        assert leak not in case
    assert "tests" not in " ".join(str(v) for k, v in case.items() if k != "code")
