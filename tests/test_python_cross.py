"""Tests del cruce Vulture + coverage (analyzers/python_cross.py).

Casos inspirados en lo que vimos de verdad (ver "Aprendizajes de
coverage.py" en CLAUDE.md): _wrap_chunks de click (Vulture "high", pero
lo llama la librería estándar) y _find de requests (muerto confirmado).
"""

import pytest

from deadcode_service.analyzers.python_cross import (
    CONFIRMED_SUFFIX,
    FALSE_POSITIVE_REASON,
    cross,
    execution_of,
    summarize_cross,
)


def finding(name, type_="method", line=10, last_line=12, priority="medium", file="src/m.py", confidence=60):
    return {
        "file": file, "line": line, "last_line": last_line, "type": type_, "name": name,
        "confidence": confidence, "priority": priority, "reason": "Público sin usos",
    }


def cov(executed=(), missing=(), file="src/m.py"):
    return {"percent": 50.0, "files": {file: {"executed": list(executed), "missing": list(missing)}}}


# --- ¿Se ejecutó? -----------------------------------------------------


def test_la_linea_del_def_no_cuenta_solo_el_cuerpo():
    """La línea 10 (def) se ejecuta SIEMPRE al importar: no significa uso."""
    files = cov(executed=[10], missing=[11, 12])["files"]
    assert execution_of(finding("f"), files) is False


def test_cuerpo_ejecutado():
    files = cov(executed=[10, 11], missing=[12])["files"]
    assert execution_of(finding("f"), files) is True


def test_lineas_fuera_de_la_funcion_no_cuentan():
    """Lo ejecutado ANTES o DESPUÉS de la función no es de ella."""
    files = cov(executed=[9, 13], missing=[11])["files"]
    assert execution_of(finding("f"), files) is False


@pytest.mark.parametrize("type_", ["import", "variable", "attribute", "class", "unreachable_code"])
def test_coverage_no_opina_si_no_es_funcion(type_):
    """Ejecutar "x = 1" o un import no dice si alguien los LEE."""
    files = cov(executed=[10, 11, 12])["files"]
    assert execution_of(finding("x", type_=type_), files) is None


def test_archivo_no_medido():
    files = cov(executed=[11], file="src/otro.py")["files"]
    assert execution_of(finding("f", file="tests/test_m.py"), files) is None


def test_cuerpo_sin_lineas_medibles():
    """Ej. un cuerpo que es solo un docstring o "...": coverage no lo mide."""
    files = cov(executed=[10], missing=[20])["files"]
    assert execution_of(finding("f", last_line=11), files) is None


@pytest.mark.parametrize("type_", ["function", "method", "property"])
def test_funciones_metodos_y_propiedades_si_se_cruzan(type_):
    files = cov(missing=[11])["files"]
    assert execution_of(finding("f", type_=type_), files) is False


# --- Veredicto: prioridad y motivo --------------------------------------


def test_falso_positivo_baja_a_low_aunque_vulture_dijera_high():
    """Como _wrap_chunks de click: "high" para Vulture, pero se ejecutó."""
    [result] = cross([finding("_wrap_chunks", priority="high")], cov(executed=[11]))

    assert result["executed"] is True
    assert result["static_priority"] == "high"  # se conserva lo que dijo Vulture
    assert result["priority"] == "low"
    assert result["reason"] == FALSE_POSITIVE_REASON


def test_sin_ejecutar_no_sube_la_prioridad():
    """"Nunca ejecutado" casi siempre es código vivo sin tests (etapa 8):
    no se promete más de lo que se sabe."""
    [result] = cross([finding("list_domains", priority="medium")], cov(missing=[11, 12]))

    assert result["executed"] is False
    assert (result["static_priority"], result["priority"]) == ("medium", "medium")
    assert result["reason"] == "Público sin usos" + CONFIRMED_SUFFIX


def test_sin_ejecutar_high_y_low_no_cambian():
    """Como _find de requests (high). Un "low" (ej. parámetro, tests) no
    sube: que no se ejecute no cambia por qué era poco importante."""
    high, low = cross(
        [finding("_find", priority="high", line=10), finding("__x__", priority="low", line=20, last_line=21)],
        cov(missing=[11, 21]),
    )
    assert (high["name"], high["priority"]) == ("_find", "high")
    assert (low["name"], low["priority"]) == ("__x__", "low")


def test_motivo_sin_punto_doble():
    item = {**finding("f"), "reason": "Privado sin usos."}
    [result] = cross([item], cov(missing=[11]))
    assert result["reason"] == "Privado sin usos" + CONFIRMED_SUFFIX
    assert ".." not in result["reason"]


def test_no_aplica_queda_igual():
    original = finding("x", type_="variable", priority="medium")
    [result] = cross([original], cov(executed=[10, 11]))

    assert result == {**original, "static_priority": "medium", "executed": None}


def test_se_reordena_por_la_nueva_prioridad():
    findings = [
        finding("era_high", priority="high", line=10, last_line=11),  # se ejecutó -> low
        finding("era_medium", priority="medium", line=20, last_line=21),  # nunca -> high
    ]
    result = cross(findings, cov(executed=[11], missing=[21]))

    assert [f["name"] for f in result] == ["era_medium", "era_high"]


def test_sin_datos_de_coverage_nadie_cambia():
    findings = [finding("f", priority="high")]
    [result] = cross(findings, {"percent": 0, "files": {}})
    assert (result["priority"], result["executed"]) == ("high", None)


def test_resumen():
    findings = cross(
        [finding("a", line=10, last_line=11), finding("b", line=20, last_line=21),
         finding("c", type_="import", line=30, last_line=30)],
        cov(executed=[11], missing=[21]),
    )
    assert summarize_cross(findings) == {"confirmed": 1, "false_positives": 1, "not_applicable": 1}
