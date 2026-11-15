"""Tests del analizador estático (Vulture) sobre código de ejemplo."""

from deadcode_service.analyzers import python_static

APP_FASTAPI = '''
from fastapi import FastAPI

app = FastAPI()


@app.get("/health")
def health():
    return {"status": "ok"}


def used_helper():
    return 1


def forgotten_function():
    return 2


print(used_helper())
'''


def _write(tmp_path, files: dict[str, str]):
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tmp_path


def test_detecta_codigo_muerto_y_respeta_el_framework(tmp_path):
    repo = _write(tmp_path, {"src/app.py": APP_FASTAPI})

    result = python_static.analyze(repo)

    names = [f["name"] for f in result["dead_code"]]
    assert names == ["forgotten_function"]  # health no: la usa FastAPI
    assert result["frameworks_detected"] == ["fastapi"]
    assert result["files_analyzed"] == 1

    [finding] = result["dead_code"]
    assert finding["file"] == "src/app.py"  # ruta relativa, con "/"
    assert finding["line"] == 16
    assert finding["type"] == "function"
    assert finding["confidence"] == 60


def test_sin_el_import_las_reglas_no_se_aplican(tmp_path):
    """Las reglas solo se activan si el repo usa el framework: sin
    "from fastapi import", la ruta sí se reporta."""
    code = APP_FASTAPI.replace("from fastapi import FastAPI", "FastAPI = object")
    repo = _write(tmp_path, {"app.py": code})

    names = [f["name"] for f in python_static.analyze(repo)["dead_code"]]

    assert "health" in names


def test_detecta_varios_frameworks(tmp_path):
    repo = _write(
        tmp_path,
        {
            "a.py": "import pytest\n",
            "b.py": "from pydantic import BaseModel\n",
            "c.py": "from django.db import models\n",
            "d.py": "import fastapi_utils  # no es fastapi\n",
        },
    )

    result = python_static.analyze(repo)

    assert result["frameworks_detected"] == ["django", "pydantic", "pytest"]


def test_ordena_por_prioridad_y_resume(tmp_path):
    code = "import os\n\n\ndef nadie_me_llama():\n    pass\n"
    repo = _write(tmp_path, {"m.py": code})

    result = python_static.analyze(repo)

    assert [f["type"] for f in result["dead_code"]] == ["import", "function"]
    assert [f["priority"] for f in result["dead_code"]] == ["high", "medium"]
    assert result["summary"] == {
        "total": 2,
        "by_priority": {"high": 1, "medium": 1, "low": 0},
        "by_location": {"code": 2, "tests": 0, "docs": 0, "examples": 0},
    }
    assert result["analyzer"] == "vulture"
    assert result["analyzer_version"] == "2.16"


def test_ignora_carpetas_de_entornos_virtuales(tmp_path):
    repo = _write(
        tmp_path,
        {"app.py": "x = 1\nprint(x)\n", ".venv/lib/dep.py": "def ajena():\n    pass\n"},
    )

    names = [f["name"] for f in python_static.analyze(repo)["dead_code"]]

    assert "ajena" not in names
