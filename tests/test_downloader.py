"""Tests de downloader_main.py (el programa del pod de descarga).

Sin internet: pip se simula con monkeypatch de _run_pip.
"""

import io
import json
import subprocess
import tarfile

import pytest

from deadcode_service import downloader_main
from deadcode_service.dependencies import DependencyError
from deadcode_service.fetcher import InvalidArchiveError
from tests.helpers import make_tarball

PYPROJECT = '[project]\nname = "x"\n[dependency-groups]\ntests = ["pytest"]\n'


def read_output(data: bytes) -> tuple[list[str], dict]:
    """(nombres en orden, resultado.json) del .tar de salida."""
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
        names = tar.getnames()
        result = json.load(tar.extractfile("resultado.json"))
    return names, result


@pytest.fixture
def fake_pip(monkeypatch):
    """pip simulado: guarda lo que le pidieron y "descarga" wheels falsas."""
    calls = []

    def run(requirements_file, dest, *, returncode=0, stderr=b"", wheel_bytes=10):
        calls.append(requirements_file.read_text(encoding="utf-8").split())
        for name in ("pytest-9.0.0-py3-none-any.whl", "coverage-7.17.3-cp312-x.whl"):
            (dest / name).write_bytes(b"w" * wheel_bytes)
        return subprocess.CompletedProcess([], returncode, b"", stderr)

    state = {"kwargs": {}}
    monkeypatch.setattr(
        downloader_main, "_run_pip", lambda req, dest: run(req, dest, **state["kwargs"])
    )
    return calls, state


# --- Leer el pyproject.toml ------------------------------------------


def test_lee_solo_el_pyproject_de_la_raiz():
    # El de la subcarpeta va PRIMERO: así el test falla si se acepta.
    tarball = make_tarball({"sub/pyproject.toml": "otro", "a.py": "", "pyproject.toml": "raiz"})
    assert downloader_main.read_pyproject(tarball) == b"raiz"


@pytest.mark.parametrize(
    "files",
    [
        {"sub/pyproject.toml": "otro", "setup.py": ""},
        # Truco: una CARPETA llamada pyproject.toml con un archivo dentro.
        {"pyproject.toml/otro": "x"},
    ],
)
def test_sin_pyproject_en_la_raiz(files):
    with pytest.raises(DependencyError, match="no tiene pyproject.toml"):
        downloader_main.read_pyproject(make_tarball(files))


def test_pyproject_demasiado_grande(monkeypatch):
    monkeypatch.setattr(downloader_main, "MAX_PYPROJECT_BYTES", 10)
    with pytest.raises(DependencyError, match="demasiado grande"):
        downloader_main.read_pyproject(make_tarball({"pyproject.toml": "x" * 11}))


@pytest.mark.parametrize("data", [b"", b"no es un tar.gz", make_tarball({"a.py": ""})[:40]])
def test_archivo_corrupto(data):
    with pytest.raises(InvalidArchiveError):
        downloader_main.read_pyproject(data)


# --- pip --------------------------------------------------------------


def test_pip_solo_wheels_solo_pypi_y_sin_configuracion_ajena():
    flags = downloader_main.PIP_DOWNLOAD_FLAGS
    assert "--only-binary=:all:" in flags  # nunca ejecutar setup.py con red
    assert "--isolated" in flags  # ignora PIP_INDEX_URL, pip.conf, etc.
    assert flags[flags.index("--index-url") + 1] == "https://pypi.org/simple"


def test_descarga_ok(fake_pip):
    calls, _ = fake_pip
    names, result = read_output(downloader_main.process(make_tarball({"pyproject.toml": PYPROJECT})))
    assert result == {"ok": True}
    assert names[0] == "resultado.json"  # primero, a propósito
    assert "requirements.txt" in names
    assert sorted(n for n in names if n.startswith("wheels/")) == [
        "wheels/coverage-7.17.3-cp312-x.whl",
        "wheels/pytest-9.0.0-py3-none-any.whl",
    ]
    assert "pytest" in calls[0]


def test_pip_falla_muestra_su_motivo(fake_pip):
    _, state = fake_pip
    state["kwargs"] = {
        "returncode": 1,
        "stderr": b"Looking in indexes...\nERROR: No matching distribution found for libquenoexiste\n",
    }
    names, result = read_output(downloader_main.process(make_tarball({"pyproject.toml": PYPROJECT})))
    assert names == ["resultado.json"]  # nada a medias
    assert result == {
        "ok": False,
        "error": "No se pudieron descargar las dependencias: "
        "No matching distribution found for libquenoexiste",
    }


def test_pip_tarda_demasiado(monkeypatch):
    def slow(req, dest):
        raise subprocess.TimeoutExpired("pip", 1)

    monkeypatch.setattr(downloader_main, "_run_pip", slow)
    _, result = read_output(downloader_main.process(make_tarball({"pyproject.toml": PYPROJECT})))
    assert "tiempo máximo" in result["error"]


def test_wheels_demasiado_grandes(fake_pip, monkeypatch):
    _, state = fake_pip
    state["kwargs"] = {"wheel_bytes": 100}
    monkeypatch.setattr(downloader_main, "MAX_WHEELS_BYTES", 150)  # 2 x 100 > 150
    names, result = read_output(downloader_main.process(make_tarball({"pyproject.toml": PYPROJECT})))
    assert names == ["resultado.json"]
    assert "pesan más de" in result["error"]


def test_dependencia_con_url_no_llega_a_pip(fake_pip):
    calls, _ = fake_pip
    evil = '[project]\nname = "x"\ndependencies = ["e @ http://10.0.0.1/e.whl"]\n'
    _, result = read_output(downloader_main.process(make_tarball({"pyproject.toml": evil})))
    assert "URL directa" in result["error"]
    assert calls == []  # pip ni siquiera se ejecutó
