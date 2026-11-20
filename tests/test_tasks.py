"""Tests de la tarea del worker.

El worker real no corre en Windows (RQ usa fork), así que llamamos a
analyze_repo directamente, igual que lo haría el worker.

monkeypatch: herramienta de pytest para reemplazar algo SOLO durante
un test. Aquí reemplazamos la DESCARGA (para no depender de internet)
o el análisis completo (para provocar errores a propósito).
"""

import uuid

import pytest

from deadcode_service import db, fetcher, sandbox, tasks
from deadcode_service.coverage_main import CoverageError
from deadcode_service.dependencies import DependencyError
from tests.helpers import make_tarball

REPO = "https://github.com/psf/requests"


def crear_analisis() -> uuid.UUID:
    analysis_id = uuid.uuid4()
    db.create_analysis(analysis_id, REPO)
    return analysis_id


def test_analisis_real_con_vulture_queda_finished(monkeypatch):
    """De punta a punta: descarga (falsa) -> extracción -> Vulture -> Postgres."""
    code = "def usada():\n    return 1\n\n\ndef olvidada():\n    return 2\n\n\nprint(usada())\n"
    monkeypatch.setattr(fetcher, "_http_get", lambda url: make_tarball({"app.py": code}, commit="f00ba4"))
    analysis_id = crear_analisis()

    tasks.analyze_repo(str(analysis_id), REPO)

    row = db.get_analysis(analysis_id)
    assert row["status"] == "finished"
    result = row["result"]
    assert result["repo_url"] == REPO
    assert result["commit"] == "f00ba4"
    assert result["files_analyzed"] == 1
    assert [f["name"] for f in result["dead_code"]] == ["olvidada"]


def test_error_esperado_guarda_mensaje_claro_sin_relanzar(monkeypatch):
    def privado(url):
        raise fetcher.RepoNotFoundError()

    monkeypatch.setattr(fetcher, "_http_get", privado)
    analysis_id = crear_analisis()

    tasks.analyze_repo(str(analysis_id), REPO)  # no lanza: no es un bug

    row = db.get_analysis(analysis_id)
    assert row["status"] == "failed"
    assert row["error"] == "El repositorio no existe o es privado"
    assert row["finished_at"] is not None


def test_error_inesperado_oculta_detalles_y_relanza(monkeypatch):
    def analisis_roto(repo_url, dynamic=False):
        raise RuntimeError("detalle interno: /tmp/deadcode-x, clave=123")

    monkeypatch.setattr(tasks, "_run_analysis", analisis_roto)
    analysis_id = crear_analisis()

    # El error debe propagarse (para que RQ lo registre en los logs)...
    with pytest.raises(RuntimeError):
        tasks.analyze_repo(str(analysis_id), REPO)

    # ...pero al usuario solo le mostramos el tipo, sin detalles internos.
    row = db.get_analysis(analysis_id)
    assert row["status"] == "failed"
    assert row["error"] == "Error inesperado (RuntimeError)"


def test_la_carpeta_temporal_se_borra(monkeypatch):
    carpetas = []
    original = fetcher.extract_python_files

    def espiar(data, dest):
        carpetas.append(dest)
        return original(data, dest)

    monkeypatch.setattr(fetcher, "_http_get", lambda url: make_tarball({"app.py": "x = 1"}))
    monkeypatch.setattr(fetcher, "extract_python_files", espiar)

    tasks._run_analysis(REPO)

    [carpeta] = carpetas
    assert not carpeta.exists()


# --- Nivel 2 (dynamic=True) ---------------------------------------------
# Los pods se simulan: aquí se prueba la UNIÓN de las piezas (cada pieza
# tiene sus propios tests y se probó en vivo en el clúster).


# "olvidada" ocupa las líneas 5-6 (su cuerpo: la 6).
CODE = "def usada():\n    return 1\n\n\ndef olvidada():\n    return 2\n\n\nprint(usada())\n"


@pytest.fixture
def nivel2(monkeypatch):
    """Descarga falsa del repo + pods de nivel 2 simulados."""
    monkeypatch.setattr(fetcher, "_http_get", lambda url: make_tarball({"app.py": CODE}))
    state = {"deps_error": None, "coverage_error": None, "coverage": None}

    def download(tarball):
        if state["deps_error"]:
            raise state["deps_error"]
        return b"deps"

    def run_coverage(tarball, deps):
        assert deps == b"deps"
        if state["coverage_error"]:
            raise state["coverage_error"]
        return state["coverage"]

    monkeypatch.setattr(sandbox, "download_dependencies", download)
    monkeypatch.setattr(sandbox, "run_coverage", run_coverage)
    return state


def _coverage(executed, missing):
    return {
        "tests": {"summary": "1 passed in 0.1s", "exit_code": 0, "timed_out": False,
                  "project_installed": True, "source": "."},
        "coverage": {"percent": 80.0, "files": {"app.py": {"executed": executed, "missing": missing}}},
    }


def test_nivel_2_cruza_y_marca_sin_ejecutar(nivel2):
    # "olvidada" (líneas 5-6): su cuerpo (6) nunca se ejecutó -> "sin ejecutar".
    nivel2["coverage"] = _coverage(executed=[1, 2, 5, 9], missing=[6])
    analysis_id = crear_analisis()

    tasks.analyze_repo(str(analysis_id), REPO, True)

    result = db.get_analysis(analysis_id)["result"]
    [finding] = result["dead_code"]
    assert (finding["name"], finding["executed"]) == ("olvidada", False)
    # "Sin ejecutar" NO es "muerto" (etapa 8): la prioridad no sube.
    assert (finding["static_priority"], finding["priority"]) == ("medium", "medium")
    assert "nunca lo ejecutaron" in finding["reason"]
    assert result["summary"]["by_priority"]["medium"] == 1  # recalculado
    assert result["dynamic"] == {
        "status": "finished",
        "coverage_percent": 80.0,
        "tests": {"summary": "1 passed in 0.1s", "exit_code": 0, "timed_out": False},
        "cross": {"confirmed": 1, "false_positives": 0, "not_applicable": 0},
    }


def test_nivel_2_falso_positivo_baja(nivel2):
    nivel2["coverage"] = _coverage(executed=[1, 2, 5, 6, 9], missing=[])
    analysis_id = crear_analisis()

    tasks.analyze_repo(str(analysis_id), REPO, True)

    [finding] = db.get_analysis(analysis_id)["result"]["dead_code"]
    assert (finding["executed"], finding["priority"]) == (True, "low")


@pytest.mark.parametrize(
    "where, error",
    [
        ("deps_error", DependencyError("El repo no tiene pyproject.toml: ...")),
        ("coverage_error", CoverageError("Los tests del repo no pudieron ejecutarse: KeyError: 'X'")),
    ],
)
def test_si_el_nivel_2_falla_igual_entrega_el_nivel_1(nivel2, where, error):
    nivel2[where] = error
    analysis_id = crear_analisis()

    tasks.analyze_repo(str(analysis_id), REPO, True)

    row = db.get_analysis(analysis_id)
    assert row["status"] == "finished"  # NO "failed": lo de Vulture sirve igual
    [finding] = row["result"]["dead_code"]
    assert finding["name"] == "olvidada" and "executed" not in finding
    assert row["result"]["dynamic"] == {"status": "failed", "error": str(error)}


def test_un_bug_del_nivel_2_no_tumba_el_nivel_1(nivel2, caplog):
    nivel2["coverage_error"] = RuntimeError("detalle interno")
    analysis_id = crear_analisis()

    tasks.analyze_repo(str(analysis_id), REPO, True)

    row = db.get_analysis(analysis_id)
    assert row["status"] == "finished"
    assert row["result"]["dynamic"] == {"status": "failed", "error": "Error inesperado (RuntimeError)"}
    assert "detalle interno" in caplog.text  # el detalle, solo en los logs


def test_nivel_1_no_toca_el_nivel_2(nivel2):
    nivel2["deps_error"] = AssertionError("no debía llamarse")
    analysis_id = crear_analisis()

    tasks.analyze_repo(str(analysis_id), REPO)  # como los trabajos viejos: 2 argumentos

    result = db.get_analysis(analysis_id)["result"]
    assert "dynamic" not in result
