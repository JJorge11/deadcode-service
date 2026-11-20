"""Tests de la corrección del examen (scripts/eval_triage.py)."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "eval_triage.py"


def _load():
    spec = importlib.util.spec_from_file_location("eval_triage", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ev = _load()
M, FP = "muerto", "falso_positivo"


def test_nota_perfecta():
    n = ev.score([M, FP, M], [M, FP, M])
    assert n["acierto"] == 1 and n["acierto_balanceado"] == 1 and n["confianza_en_muerto"] == 1


def test_responder_siempre_lo_mismo_saca_50_balanceado():
    """Aunque "muerto" sea mayoría (3 de 4), el acierto simple engaña (75%)
    y el balanceado no (50%)."""
    n = ev.score([M, M, M, FP], [M, M, M, M])

    assert n["acierto"] == 0.75
    assert n["acierto_balanceado"] == 0.5
    assert n["detecta_falsos_positivos"] == 0


def test_confianza_en_muerto():
    # Dijo "muerto" 4 veces y acertó 1: decirle al usuario "bórralo"
    # sería un error 3 de cada 4 veces.
    n = ev.score([M, FP, FP, FP], [M, M, M, M])
    assert n["confianza_en_muerto"] == 0.25


def test_nunca_dijo_muerto():
    assert ev.score([M, FP], [FP, FP])["confianza_en_muerto"] is None


def test_respuesta_invalida_cuenta_como_error():
    n = ev.score([M, FP], ["invalido", FP])
    assert n["acierto"] == 0.5 and n["invalidas"] == 1


def test_el_motor_nunca_ve_la_respuesta():
    seen = {}

    def espia(case):
        seen.update(case)
        return {"veredicto": M}

    ev.ask(espia, {"id": "x", "code": "def f(): pass", "label": "muerto_probable", "split": "final"})

    assert "label" not in seen and "split" not in seen and seen["id"] == "x"


@pytest.mark.parametrize("answer", [{}, {"veredicto": "dead"}, {"veredicto": None}])
def test_respuesta_rara_del_motor_se_marca_invalida(answer):
    assert ev.ask(lambda case: answer, {"id": "x"})["veredicto"] == "invalido"


def test_el_examen_real_carga_y_tiene_ambos_grupos():
    cases = ev.load_exam()
    assert {c["split"] for c in cases} == {"practica", "final"}
    assert {c["label"] for c in cases} == set(ev.TRUTH)
    assert len(ev.load_exam(split="final")) + len(ev.load_exam(split="practica")) == len(cases)


# --- Revisión humana -----------------------------------------------------------


def test_la_revision_manda_sobre_la_etiqueta_del_nivel_2():
    revision = {"a": {"etiqueta": "vivo"}, "b": {"etiqueta": "muerto"}, "c": {"etiqueta": "dudoso"}}
    dead = {"label": "muerto_probable"}

    assert ev.truth_of({"id": "a", **dead}, revision) == FP      # "nunca ejecutado" pero vivo
    assert ev.truth_of({"id": "b", **dead}, revision) == M
    assert ev.truth_of({"id": "c", **dead}, revision) is None    # no cuenta
    assert ev.truth_of({"id": "z", **dead}, revision) == M       # sin revisar: nivel 2


def test_los_dudosos_no_entran_en_la_nota():
    cases = [{"id": "a", "label": "muerto_probable"}, {"id": "b", "label": "falso_positivo"}]

    result = ev.run("siempre_fp", ev.siempre_fp, cases, {"a": {"etiqueta": "dudoso"}})

    assert result["notas"]["casos"] == 1 and result["notas"]["dudosos_excluidos"] == 1
    assert [r["id"] for r in result["respuestas"]] == ["b"]


def test_sin_casos_de_una_clase_no_hay_acierto_balanceado():
    """Un 100% "balanceado" con una sola clase sería engañoso."""
    assert ev.score([FP, FP], [FP, FP])["acierto_balanceado"] is None


def test_la_revision_real_solo_toca_casos_del_examen():
    ids = {c["id"] for c in ev.load_exam()}
    revision = ev.load_revision()
    assert revision and set(revision) <= ids
    assert {r["etiqueta"] for r in revision.values()} <= set(ev.REVIEWED)
    assert all(r["motivo"] for r in revision.values())
