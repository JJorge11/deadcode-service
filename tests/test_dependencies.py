"""Tests de dependencies.py: qué librerías se descargan para los tests.

Casos sacados de repos reales (ver "Aprendizajes de coverage.py" en
CLAUDE.md) y de los trucos que un repo malicioso podría intentar.
"""

import pytest

from deadcode_service.dependencies import (
    COVERAGE_REQUIREMENT,
    MAX_REQUIREMENTS,
    DependencyError,
    requirements_for_tests as requirements_for,
)


def reqs(toml: str) -> list[str]:
    return requirements_for(toml.encode("utf-8"))


def test_como_itsdangerous_grupo_tests_y_empaquetador():
    result = reqs("""
[project]
name = "itsdangerous"
dependencies = []
[dependency-groups]
dev = ["ruff", "tox"]
tests = ["freezegun", "pytest"]
[build-system]
requires = ["flit_core<4"]
""")
    # "tests" gana a "dev"; pytest no se repite; coverage = nuestra versión.
    assert result == ["freezegun", "pytest", "flit_core<4", COVERAGE_REQUIREMENT]


def test_como_requests_se_nombra_a_si_mismo_con_extras():
    result = reqs("""
[project]
name = "Requests"
dependencies = ["idna>=2.5,<4", "urllib3>=1.26,<3"]
[project.optional-dependencies]
socks = ["PySocks>=1.5.6, !=1.5.7"]
[dependency-groups]
test = ["requests[socks]", "pytest>=3", "pytest-cov"]
[build-system]
requires = ["setuptools>=61.0"]
""")
    # "requests[socks]" (el repo mismo) se cambia por sus extras: PySocks.
    assert "PySocks!=1.5.7,>=1.5.6" in result
    assert not any(r.lower().startswith("requests") for r in result)
    assert "idna<4,>=2.5" in result
    assert "pytest>=3" in result and "pytest" not in result  # no agrega otro pytest
    assert result[-1] == COVERAGE_REQUIREMENT


def test_include_group_y_ciclos():
    result = reqs("""
[project]
name = "x"
[dependency-groups]
base = ["attrs", {include-group = "tests"}]
tests = ["hypothesis", {include-group = "base"}]
""")
    # tests -> base -> (tests: ciclo, se corta)
    assert result[:2] == ["hypothesis", "attrs"]


def test_sin_grupo_de_test_usa_dependencias_y_agrega_pytest():
    result = reqs('[project]\nname = "x"\ndependencies = ["click"]\n')
    # Sin [build-system]: el empaquetador por defecto de pip.
    assert result == ["click", "setuptools>=40.8.0", "pytest", COVERAGE_REQUIREMENT]


def test_la_coverage_del_repo_se_reemplaza_por_la_nuestra():
    result = reqs("""
[project]
name = "x"
[dependency-groups]
tests = ["coverage[toml]==6.0", "pytest"]
""")
    assert [r for r in result if r.startswith("coverage")] == [COVERAGE_REQUIREMENT]


def test_se_conservan_los_marcadores():
    result = reqs('[project]\nname = "x"\ndependencies = ["tomli; python_version < \'3.11\'"]\n')
    assert 'tomli; python_version < "3.11"' in result


@pytest.mark.parametrize(
    "dep",
    [
        "evil @ https://example.com/evil.whl",
        "evil @ http://169.254.169.254/latest/meta-data/",
        "evil @ file:///etc/passwd",
        "evil @ git+https://github.com/x/y",
    ],
)
def test_rechaza_urls_directas(dep):
    with pytest.raises(DependencyError, match="URL directa"):
        reqs(f'[project]\nname = "x"\ndependencies = ["{dep}"]\n')


def test_rechaza_url_escondida_en_el_empaquetador_o_en_un_extra():
    with pytest.raises(DependencyError, match="URL directa"):
        reqs('[project]\nname = "x"\n[build-system]\nrequires = ["b @ https://e.com/b.whl"]\n')
    with pytest.raises(DependencyError, match="URL directa"):
        reqs("""
[project]
name = "x"
[project.optional-dependencies]
extra = ["evil @ https://e.com/e.whl"]
[dependency-groups]
tests = ["x[extra]"]
""")


@pytest.mark.parametrize(
    "toml",
    [
        "esto no es toml [",
        '[project]\nname = "x"\ndependencies = "click"\n',  # texto en vez de lista
        '[project]\nname = "x"\ndependencies = [1]\n',
        '[project]\nname = "x"\ndependencies = ["--index-url http://e.com"]\n',
        '[project]\nname = "x"\ndependencies = ["-e ."]\n',
        'project = "x"\n',
        '[project]\nname = "x"\n[dependency-groups]\ntests = [{otra = "cosa"}]\n',
    ],
)
def test_rechaza_pyproject_invalido(toml):
    with pytest.raises(DependencyError):
        reqs(toml)


def test_bytes_que_no_son_utf8():
    with pytest.raises(DependencyError, match="no es válido"):
        requirements_for(b"\xff\xfe[project]")


def test_demasiadas_dependencias():
    deps = ", ".join(f'"lib{i}"' for i in range(MAX_REQUIREMENTS + 1))
    with pytest.raises(DependencyError, match="demasiadas"):
        reqs(f'[project]\nname = "x"\ndependencies = [{deps}]\n')
