"""Corrige el examen de triaje (evals/triage/examen.jsonl) con un "motor".

Motor = una forma de responder cada caso: "muerto" o "falso_positivo".
Hoy solo hay motores SIN IA (líneas base, paso 8.2); los de IA (Ollama,
Groq...) se agregan en el paso 8.3 con la MISMA interfaz:
    motor(caso) -> {"veredicto": "muerto" | "falso_positivo", ...}
El motor NUNCA ve "label" (la respuesta): se le quita antes de llamarlo.

Uso:
    python scripts/eval_triage.py                    # todos los motores sin IA
    python scripts/eval_triage.py --motor vulture_alta --split final
    python scripts/eval_triage.py --guardar          # deja el detalle en evals/triage/resultados/
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
EXAM = ROOT / "evals" / "triage" / "examen.jsonl"
# Revisión HUMANA de los "muerto_probable" (muchos eran código vivo sin tests).
REVISION = ROOT / "evals" / "triage" / "revision.json"
RESULTS = ROOT / "evals" / "triage" / "resultados"

# Respuesta del examen -> veredicto del motor.
TRUTH = {"muerto_probable": "muerto", "falso_positivo": "falso_positivo"}
# Etiqueta de la revisión humana -> veredicto correcto (None = no cuenta).
REVIEWED = {"muerto": "muerto", "vivo": "falso_positivo", "dudoso": None}
VERDICTS = ("muerto", "falso_positivo")

Engine = Callable[[dict], dict]


# --- Motores SIN IA (líneas base) --------------------------------------------


def siempre_muerto(case: dict) -> dict:
    return {"veredicto": "muerto"}


def siempre_fp(case: dict) -> dict:
    return {"veredicto": "falso_positivo"}


def vulture_alta(case: dict) -> dict:
    """Lo que hoy le decimos al usuario: prioridad ALTA = revisa esto."""
    return {"veredicto": "muerto" if case["static_priority"] == "high" else "falso_positivo"}


def vulture_alta_media(case: dict) -> dict:
    return {"veredicto": "muerto" if case["static_priority"] in ("high", "medium") else "falso_positivo"}


ENGINES: dict[str, Engine] = {
    "siempre_muerto": siempre_muerto,
    "siempre_fp": siempre_fp,
    "vulture_alta": vulture_alta,
    "vulture_alta_media": vulture_alta_media,
}


# --- Corrección -------------------------------------------------------------------


def load_exam(path: Path = EXAM, split: str | None = None) -> list[dict]:
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [c for c in cases if split in (None, "todo", c["split"])]


def load_revision(path: Path = REVISION) -> dict[str, dict]:
    return json.loads(path.read_text(encoding="utf-8"))["casos"] if path.exists() else {}


def truth_of(case: dict, revision: dict[str, dict]) -> str | None:
    """Respuesta correcta del caso: la de la revisión humana si existe; si
    no, la del nivel 2. None = caso dudoso, no entra en la nota."""
    if case["id"] in revision:
        return REVIEWED[revision[case["id"]]["etiqueta"]]
    return TRUTH[case["label"]]


def ask(engine: Engine, case: dict) -> dict:
    """Llama al motor SIN la respuesta y valida lo que devuelve."""
    hidden = {k: v for k, v in case.items() if k not in ("label", "split")}
    answer = engine(hidden)
    if answer.get("veredicto") not in VERDICTS:
        # Respuesta rota (una IA puede devolver cualquier cosa): cuenta
        # como error, no se adivina.
        answer = {**answer, "veredicto": "invalido"}
    return answer


def score(truths: list[str], predictions: list[str]) -> dict:
    """Notas del examen. truths/predictions: "muerto" | "falso_positivo"
    (predictions puede traer "invalido": siempre cuenta como error)."""
    total = len(truths)
    pairs = list(zip(truths, predictions))
    hits = sum(t == p for t, p in pairs)

    def recall(cls: str) -> float | None:
        of_class = [p for t, p in pairs if t == cls]
        return sum(p == cls for p in of_class) / len(of_class) if of_class else None

    said_dead = [t for t, p in pairs if p == "muerto"]
    recalls = (recall("muerto"), recall("falso_positivo"))
    return {
        "casos": total,
        "acierto": hits / total if total else None,
        # Promedio del acierto en CADA clase: responder siempre lo mismo
        # saca 50%, aunque una clase sea mayoría. Nota PRINCIPAL.
        # Si el grupo no tiene casos de alguna clase, no hay "balance" que
        # medir: None (mejor sin nota que una nota engañosa).
        "acierto_balanceado": sum(recalls) / 2 if None not in recalls else None,
        "detecta_muertos": recall("muerto"),
        "detecta_falsos_positivos": recall("falso_positivo"),
        # Cuando dice "muerto", ¿qué % lo era? (un "muerto" equivocado puede
        # llevar a BORRAR código vivo). None si nunca dijo "muerto".
        "confianza_en_muerto": sum(t == "muerto" for t in said_dead) / len(said_dead) if said_dead else None,
        "invalidas": sum(p == "invalido" for p in predictions),
    }


def run(engine_name: str, engine: Engine, cases: list[dict], revision: dict[str, dict] | None = None) -> dict:
    revision = load_revision() if revision is None else revision
    graded = [(c, truth_of(c, revision)) for c in cases]
    graded = [(c, t) for c, t in graded if t is not None]  # sin los dudosos
    start = time.monotonic()
    answers = [ask(engine, c) for c, _ in graded]
    seconds = time.monotonic() - start
    notes = score([t for _, t in graded], [a["veredicto"] for a in answers])
    notes["dudosos_excluidos"] = len(cases) - len(graded)
    return {
        "motor": engine_name,
        "notas": notes,
        "segundos": round(seconds, 2),
        "respuestas": [{"id": c["id"], "esperado": t, **a} for (c, t), a in zip(graded, answers)],
    }


def _pct(value: float | None) -> str:
    return "   –" if value is None else f"{value * 100:3.0f}%"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--motor", choices=sorted(ENGINES), action="append",
                        help="repetible; por defecto, todos")
    parser.add_argument("--split", choices=("practica", "final", "todo"), default="practica",
                        help="practica (por defecto) para mejorar; final SOLO para la nota final")
    parser.add_argument("--guardar", action="store_true", help="guardar el detalle en evals/triage/resultados/")
    args = parser.parse_args()

    cases = load_exam(split=args.split)
    revision = load_revision()
    truths = [truth_of(c, revision) for c in cases]
    print(f"examen: {args.split}, {len(cases)} casos; cuentan {len(cases) - truths.count(None)}: "
          f"muerto={truths.count('muerto')}, vivo={truths.count('falso_positivo')} "
          f"(revisión humana en {sum(c['id'] in revision for c in cases)}; dudosos fuera: {truths.count(None)})\n")
    print(f"{'motor':22s} {'acierto':>8s} {'balanc.':>8s} {'det.muerto':>10s} {'det.FP':>7s} {'conf.muerto':>11s} {'inval.':>6s}")
    for name in args.motor or list(ENGINES):
        result = run(name, ENGINES[name], cases, revision)
        n = result["notas"]
        print(f"{name:22s} {_pct(n['acierto']):>8s} {_pct(n['acierto_balanceado']):>8s} "
              f"{_pct(n['detecta_muertos']):>10s} {_pct(n['detecta_falsos_positivos']):>7s} "
              f"{_pct(n['confianza_en_muerto']):>11s} {n['invalidas']:>6d}")
        if args.guardar:
            RESULTS.mkdir(parents=True, exist_ok=True)
            out = RESULTS / f"{name}.{args.split}.json"
            out.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
