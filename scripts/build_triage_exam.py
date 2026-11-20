"""Arma el EXAMEN de la etapa 8 (opción 2: la IA opina si un hallazgo es
código muerto) a partir de los análisis de nivel 2 ya hechos.

Cada caso = un hallazgo de Vulture (función, método o propiedad) cuyo
veredicto conocemos por la cobertura de los tests del repo:
  - "falso_positivo": Vulture dijo "no se usa" pero los tests SÍ lo
    ejecutaron (etiqueta SEGURA: si se ejecutó, está vivo).
  - "muerto_probable": Vulture dijo "no se usa" y los tests NUNCA lo
    ejecutaron (etiqueta PROBABLE: podría ser código vivo sin tests).
Lo que NO va en el caso, para que el examen no "sople" la respuesta: la
prioridad y el motivo DESPUÉS del cruce, ni nada de la cobertura.

El código de cada caso se descarga del commit EXACTO que se analizó.
Lee código ajeno (nunca lo ejecuta), así que se corre DENTRO de un
contenedor (regla del proyecto). Comando en evals/triage/README.md.

Uso:  python scripts/build_triage_exam.py --api URL --out evals/triage/examen.jsonl [--id ID ...]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
import urllib.request
from collections import Counter
from pathlib import Path

from deadcode_service import fetcher
from deadcode_service.validation import split_github_url

LABELS = {True: "falso_positivo", False: "muerto_probable"}
MAX_CODE_LINES = 60  # funciones más largas se recortan (el examen debe ser barato)
MAX_DECORATOR_LINES = 5
FINAL_SHARE = 0.30  # ~30% de los casos van al examen FINAL (por repo)
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
COMMIT_URL = "https://codeload.github.com/{owner}/{repo}/tar.gz/{commit}"


# --- Partes puras (con tests en tests/test_triage_exam.py) ------------------


def label_of(finding: dict) -> str | None:
    """Etiqueta del caso, o None si coverage no opinó (imports, clases...)."""
    return LABELS.get(finding.get("executed"))


def cut_code(lines: list[str], line: int, last_line: int) -> str:
    """El trozo de código del hallazgo, con número de línea: decoradores
    de arriba + desde el def hasta last_line (recortado si es muy largo).
    `line` y `last_line` cuentan desde 1, como en Vulture."""
    start = line - 1
    # Decoradores justo encima (@property, @app.route...): dicen mucho.
    while start > 0 and line - start <= MAX_DECORATOR_LINES and lines[start - 1].lstrip().startswith("@"):
        start -= 1
    end = min(last_line, len(lines))
    shown = list(range(start, min(end, start + MAX_CODE_LINES)))
    out = [f"{i + 1:5d}| {lines[i].rstrip()}" for i in shown]
    if end > start + MAX_CODE_LINES:
        out.append(f"     | ... ({end - start - MAX_CODE_LINES} líneas más)")
    return "\n".join(out)


def enclosing_class(lines: list[str], line: int) -> str | None:
    """Nombre de la clase que contiene la línea (para métodos), o None."""
    target = lines[line - 1]
    indent = len(target) - len(target.lstrip())
    for i in range(line - 2, -1, -1):
        text = lines[i]
        if not text.strip():
            continue
        own = len(text) - len(text.lstrip())
        if own < indent:
            match = re.match(r"\s*class\s+(\w+)", text)
            if match:
                return match.group(1)
            indent = own  # sigue subiendo (ej. función dentro de función)
            if own == 0:
                return None
    return None


def split_by_repo(counts: dict[str, int], share: float = FINAL_SHARE) -> set[str]:
    """Qué repos van al examen FINAL. Por REPO, no por caso: si un mismo
    repo estuviera en los dos, mejorar el prompt mirando la práctica
    "enseñaría" el estilo de ese repo y la nota final saldría inflada.
    Determinista: orden por huella (sha256) del nombre."""
    total = sum(counts.values())
    final: set[str] = set()
    taken = 0
    for repo in sorted(counts, key=lambda r: hashlib.sha256(r.encode()).hexdigest()):
        if taken >= share * total:
            break
        final.add(repo)
        taken += counts[repo]
    return final


def make_case(repo: str, commit: str, finding: dict, lines: list[str]) -> dict:
    return {
        "id": f"{repo}:{finding['file']}:{finding['line']}:{finding['name']}",
        "repo": repo,
        "commit": commit,
        "file": finding["file"],
        "line": finding["line"],
        "last_line": finding["last_line"],
        "name": finding["name"],
        "type": finding["type"],
        "class": enclosing_class(lines, finding["line"]),
        # Lo que Vulture y nuestra clasificación dijeron ANTES del cruce.
        "static_priority": finding["static_priority"],
        "confidence": finding["confidence"],
        "visibility": finding["visibility"],
        "location": finding["location"],
        "code": cut_code(lines, finding["line"], finding["last_line"]),
        "label": label_of(finding),
    }


# --- Partes con red ------------------------------------------------------------


def _get_json(url: str):
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def latest_level2(api: str, extra_ids: list[str] = ()) -> dict[str, dict]:
    """El análisis de nivel 2 más NUEVO de cada repo (terminado bien).
    GET /analyses trae solo los 20 más recientes (a propósito, para la
    página web): los más viejos se piden por id con --id."""
    items = _get_json(f"{api}/analyses")  # de más nuevo a más viejo
    for analysis_id in extra_ids:
        full = _get_json(f"{api}/analyses/{analysis_id}")
        dynamic = (full.get("result") or {}).get("dynamic") or {}
        items.append({**full, "dynamic_status": dynamic.get("status")})
    chosen: dict[str, dict] = {}
    for item in items:
        if item["status"] == "finished" and item.get("dynamic_status") == "finished":
            owner, name = split_github_url(item["repo_url"])
            chosen.setdefault(f"{owner}/{name}", item)
    return chosen


def download_at(repo: str, commit: str) -> bytes:
    """El .tar.gz del commit EXACTO analizado (no el de hoy: el código
    pudo cambiar). Mismas defensas que el worker: solo GitHub, límites."""
    if not COMMIT_RE.fullmatch(commit or ""):
        raise ValueError(f"commit inválido: {commit!r}")
    owner, name = repo.split("/")
    return fetcher._http_get(COMMIT_URL.format(owner=owner, repo=name, commit=commit))


def build(api: str, extra_ids: list[str] = ()) -> list[dict]:
    cases: list[dict] = []
    for repo, item in sorted(latest_level2(api, extra_ids).items()):
        result = _get_json(f"{api}/analyses/{item['id']}")["result"]
        labeled = [f for f in result["dead_code"] if label_of(f)]
        if not labeled:
            print(f"  {repo}: 0 casos con veredicto (se omite)", file=sys.stderr)
            continue
        commit = result["commit"]
        with tempfile.TemporaryDirectory() as tmp:
            root = fetcher.extract_python_files(download_at(repo, commit), Path(tmp)).path
            for finding in labeled:
                lines = (root / finding["file"]).read_text(encoding="utf-8", errors="replace").splitlines()
                cases.append(make_case(repo, commit, finding, lines))
        print(f"  {repo}: {len(labeled)} casos", file=sys.stderr)
    final = split_by_repo(Counter(c["repo"] for c in cases))
    for case in cases:
        case["split"] = "final" if case["repo"] in final else "practica"
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://127.0.0.1:8080")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--id", dest="ids", action="append", default=[],
                        help="id de un análisis más viejo que no sale en la lista (repetible)")
    args = parser.parse_args()
    cases = build(args.api.rstrip("/"), args.ids)
    with args.out.open("w", encoding="utf-8", newline="\n") as out:
        for case in sorted(cases, key=lambda c: c["id"]):
            out.write(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n")
    by = Counter((c["split"], c["label"]) for c in cases)
    print(f"{len(cases)} casos -> {args.out}", file=sys.stderr)
    for (split, label), n in sorted(by.items()):
        print(f"  {split:9s} {label:16s} {n}", file=sys.stderr)


if __name__ == "__main__":
    main()
