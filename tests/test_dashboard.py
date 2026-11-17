"""Tests del dashboard de Grafana (scripts/make_dashboard.py -> JSON)."""

import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "make_dashboard.py"
JSON_FILE = ROOT / "charts" / "deadcode-service" / "dashboards" / "deadcode.json"


def _load_script():
    spec = importlib.util.spec_from_file_location("make_dashboard", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_el_json_esta_al_dia_con_el_script():
    """Si falla: alguien editó uno sin regenerar el otro. Arreglo:
    uv run python scripts/make_dashboard.py --write"""
    assert JSON_FILE.read_text(encoding="utf-8") == _load_script().render()


def test_ids_de_paneles_unicos():
    panels = json.loads(JSON_FILE.read_text(encoding="utf-8"))["panels"]
    ids = [p["id"] for p in panels]
    assert len(ids) == len(set(ids))


def test_las_metricas_usadas_existen_en_el_codigo():
    """Un panel con un nombre de métrica mal escrito queda vacío sin
    avisar. Aquí comprobamos que cada deadcode_* aparece en el código."""
    def base(name):
        # Prometheus agrega sufijos a algunos tipos: _total, _bucket, _sum, _count.
        return re.sub(r"_(total|bucket|sum|count)$", "", name)

    source = "".join(
        p.read_text(encoding="utf-8") for p in (ROOT / "src" / "deadcode_service").glob("*metrics.py")
    )
    # Nombres EXACTOS declarados en el código (entre comillas).
    defined = {base(name) for name in re.findall(r'"(deadcode_[a-z0-9_]+)"', source)}
    dashboard = JSON_FILE.read_text(encoding="utf-8")
    used = {base(name) for name in re.findall(r"deadcode_[a-z0-9_]+", dashboard)} - {"deadcode_service"}

    assert sorted(used - defined) == []
